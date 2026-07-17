"""Lightweight browser UI fallback (no tkinter required)."""

from __future__ import annotations

import html
import json
import logging
import mimetypes
import os
import platform
import re
import signal
import subprocess
import threading
import time
import webbrowser
import concurrent.futures
from faceit_ai.multipart_form import MultipartForm, parse_multipart
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import yaml
from sqlalchemy import create_engine, func, select, text

from faceit_ai import __version__ as APP_VERSION
from faceit_ai.i18n import COOKIE_NAME as LANG_COOKIE
from faceit_ai.i18n import DEFAULT_LANG, i18n_bootstrap_script, lang_from_cookie_header, t as _t
from faceit_ai.integration.metadata_port import build_metadata_sync
from faceit_ai.persistence.models import Asset, AssetDecision, AssetFace, Consent, FaceEmbedding, Person
from faceit_ai.persistence.repository import ConsentRepository
from faceit_ai.persistence.session import create_engine_and_session_factory, session_scope
from faceit_ai.services.folder_ingest import resolve_ingest_destination
from faceit_ai.services.person_profile import (
    PersonProfile,
    apply_profile_to_person,
    existing_person_folder,
    default_display_name,
    display_name_from_slug,
    fold_german_umlauts,
    folder_slug,
    merge_tags,
    cycle_tag_consent,
    tags_to_dicts,
    profile_for_folder,
    read_person_json,
    sync_person_profile_to_db,
    tags_from_json,
    write_person_json,
)
from faceit_ai.services.processing_runs import (
    asset_path_in_folder,
    folder_path_prefixes,
    list_active_runs,
    release_folder_claims,
    this_host,
)
from faceit_ai.services.redecide_and_sync_person import run_redecide_and_sync_person
from faceit_ai.services.review_confirm import (
    FaceAssignment,
    batch_confirm_review_blocked,
    confirm_blocked_ok,
    confirm_review_blocked,
    confirm_review_ok,
    count_review_assets_by_status,
    list_review_assets_json,
    load_review_asset_detail,
    render_review_preview_jpeg,
    save_review_face_assignments,
)
from faceit_ai.settings import load_settings, parse_raw_decode_size, resolve_config_path
from faceit_ai.vision.image_loader import list_scannable_image_paths

# Browser-displayable images for the People gallery (RAW is counted but not shown).
_GALLERY_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_FAVICON_PATH = Path(__file__).resolve().parent / "static" / "favicon.png"


def _load_people_dir_from_config() -> str:
    try:
        cfg_path = resolve_config_path()
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return str((raw.get("paths") or {}).get("people_dir") or "").strip()
    except Exception:
        return ""


def _persist_people_dir(path: str) -> None:
    """Save people folder into YAML so it survives server restarts."""
    path = path.strip()
    try:
        cfg_path = resolve_config_path()
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        raw.setdefault("paths", {})["people_dir"] = path
        cfg_path.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=False), encoding="utf-8"
        )
    except Exception as e:
        logging.getLogger("faceit_ai").warning("could not persist people_dir: %s", e)


def _people_folder_names(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    names: list[str] = []
    for d in sorted(root.iterdir()):
        if d.is_dir():
            name = d.name.strip()
            if name and not name.startswith("."):
                names.append(name)
    return names


# Photo counts walk the people tree on NAS — cache aggressively for UI responsiveness.
_PHOTO_COUNT_CACHE_TTL_S = 60.0
_photo_count_cache: dict[str, tuple[float, int, int]] = {}


def _photo_count_cache_key(person_dir: Path) -> str:
    try:
        return str(person_dir.expanduser().resolve())
    except OSError:
        return str(person_dir)


def _invalidate_people_photo_counts(person_dir: Path | None = None) -> None:
    if person_dir is None:
        _photo_count_cache.clear()
        return
    _photo_count_cache.pop(_photo_count_cache_key(person_dir), None)


def _count_photos_in_person_folder(person_dir: Path) -> tuple[int, int]:
    """Return (total_scannable, gallery_browser_count)."""
    if not person_dir.is_dir():
        return 0, 0
    key = _photo_count_cache_key(person_dir)
    now = time.monotonic()
    hit = _photo_count_cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1], hit[2]
    try:
        settings = load_settings()
        paths = list_scannable_image_paths(
            person_dir,
            extensions=settings.pipeline.image.scan_extensions(),
            ignore_filename_substrings=settings.pipeline.image.ignore_filename_substrings,
        )
    except Exception:
        paths = [p for p in person_dir.rglob("*") if p.is_file()]
    gallery = [p for p in paths if p.suffix.lower() in _GALLERY_EXTENSIONS]
    total, gallery_n = len(paths), len(gallery)
    _photo_count_cache[key] = (now + _PHOTO_COUNT_CACHE_TTL_S, total, gallery_n)
    return total, gallery_n


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.logs: deque[str] = deque(maxlen=4000)
        self.activity: deque[str] = deque(maxlen=500)
        self.config_path = resolve_config_path()
        self.people_root_last = _load_people_dir_from_config()
        self.analyze_folder_last = ""
        self.ingest_dest_last = ""
        # Persist last "Analyze batch" form values so a rerun keeps selections.
        self.export_mode_last = "off"
        self.force_last = False
        self.sync_metadata_last = False
        self.status = "Idle"
        self.stage = "Waiting"
        self.warnings = 0
        self.errors = 0
        self.current_task = ""
        self.started_at = 0.0
        self.ended_at = 0.0
        self.summary: dict[str, int] = {}
        self.progress_line = ""
        self.status_counts: dict[str, int] = {"blocked": 0, "review": 0, "ok": 0}
        # True once we have meaningful Blocked/Review/OK counts to show (live or final).
        self.status_counts_ready = False
        self.run_scope_type: str | None = None
        self.run_scope_value: str = ""
        # After True, ignore stray "loading model" log lines so stage stays "Analyzing photos".
        self.analyze_phase_started = False
        self._last_live_counts_ts: float = 0.0
        self.current_proc: subprocess.Popen[str] | None = None
        self.stop_requested = False
        # Last known People mismatch warn (avoid full NAS walk on every page header).
        self.people_has_mismatch = False

    def add_log(self, line: str) -> None:
        with self.lock:
            self.logs.append(line.rstrip("\n"))

    def add_activity(self, msg: str) -> None:
        with self.lock:
            self.activity.append(msg)

    def set_progress_line(self, line: str) -> None:
        with self.lock:
            self.progress_line = line

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "running": self.running,
                "logs": list(self.logs),
                "activity": list(self.activity),
                "people_root_last": self.people_root_last,
                "analyze_folder_last": self.analyze_folder_last,
                "ingest_dest_last": self.ingest_dest_last,
                "export_mode_last": self.export_mode_last,
                "force_last": self.force_last,
                "sync_metadata_last": self.sync_metadata_last,
                "status": self.status,
                "stage": self.stage,
                "warnings": self.warnings,
                "errors": self.errors,
                "current_task": self.current_task,
                "elapsed_s": int(
                    max(
                        0.0,
                        (self.ended_at or time.time()) - self.started_at,
                    )
                )
                if self.started_at
                else 0,
                "summary": dict(self.summary),
                "progress_line": self.progress_line,
                "status_counts": dict(self.status_counts),
                "status_counts_ready": bool(self.status_counts_ready),
                "analyze_phase_started": bool(self.analyze_phase_started),
            }

    def set_running(self, val: bool) -> None:
        with self.lock:
            self.running = val

    def reset_for_run(self, task: str) -> None:
        with self.lock:
            self.running = True
            self.status = "Running"
            self.stage = "Preparing"
            self.warnings = 0
            self.errors = 0
            self.current_task = task
            self.started_at = time.time()
            self.ended_at = 0.0
            self.summary = {}
            self.run_scope_type = None
            self.run_scope_value = ""
            self.status_counts = {"blocked": 0, "review": 0, "ok": 0}
            self.status_counts_ready = False
            self.analyze_phase_started = False
            self._last_live_counts_ts = 0.0
            self.current_proc = None
            self.stop_requested = False
            self.progress_line = ""
            self.activity.clear()
            self.activity.append(f"Starting: {task}")

    def set_current_proc(self, proc: subprocess.Popen[str] | None) -> None:
        with self.lock:
            self.current_proc = proc

    def request_stop(self) -> bool:
        """Ask the active job to stop and terminate its subprocess. Returns False if idle."""
        with self.lock:
            if not self.running:
                return False
            self.stop_requested = True
            proc = self.current_proc
        _send_stop_signal(proc)
        return True

    def finish_run(self, success: bool) -> None:
        with self.lock:
            stopped = self.stop_requested
            self.stop_requested = False
            self.current_proc = None
            self.running = False
            if stopped:
                self.status = "Stopped"
                success = False
            elif not success:
                self.status = "Failed"
            elif self.errors > 0:
                self.status = "Completed with warnings"
            elif self.warnings > 0:
                self.status = "Completed with warnings"
            else:
                self.status = "Completed"
            self.stage = "Finished"
            self.ended_at = time.time()
            self.activity.append(f"Finished: {self.status}")
            # Force Progress UI to match completion (tqdm often never emits a final 100% line).
            if stopped:
                self.progress_line = "Stopped."
            else:
                self.progress_line = _finalize_progress_line(self.progress_line, success=success)
            scope_type = self.run_scope_type
            scope_value = self.run_scope_value
            # Folder analyze: never lose the path we just processed.
            if scope_type == "folder" and not (scope_value or "").strip():
                scope_value = self.analyze_folder_last
            if not scope_type and (self.analyze_folder_last or "").strip():
                scope_type = "folder"
                scope_value = self.analyze_folder_last
            self.run_scope_type = None
            self.run_scope_value = ""

        if scope_type:
            try:
                counts = _compute_outcome_counts(scope_type, scope_value)
                with self.lock:
                    self.status_counts = counts
                    self.status_counts_ready = True
            except Exception as e:
                # Summary is best-effort; UI must not crash on query failures.
                self.add_log(f"[warn] could not fill Current Status counts: {e}")

    def compute_status_counts_if_possible(self) -> None:
        """Deprecated name: use refresh_status_counts_final for end-of-analysis refresh."""
        self.refresh_status_counts_final()

    def maybe_refresh_live_status_counts(self) -> None:
        """Throttled DB snapshot of Blocked/Review/OK while a folder analysis runs.

        Cheap enough at ~1 query every few seconds; avoids hammering SQLite on every tqdm line.
        """
        with self.lock:
            scope = (self.run_scope_type, self.run_scope_value)
            if scope[0] != "folder" or not scope[1].strip():
                return
            # Avoid flashing previous-run folder totals before this run processes files.
            if not self.analyze_phase_started:
                return
            now = time.time()
            if now - self._last_live_counts_ts < 3.0:
                return
            self._last_live_counts_ts = now

        try:
            counts = _compute_outcome_counts("folder", scope[1])
        except Exception:
            return

        with self.lock:
            self.status_counts = counts
            self.status_counts_ready = True

    def refresh_status_counts_final(self) -> None:
        """Always refresh outcome counts when analysis is finishing (final totals)."""
        with self.lock:
            scope = (self.run_scope_type, self.run_scope_value)

        if not scope[0]:
            return

        try:
            counts = _compute_outcome_counts(scope[0], scope[1])
        except Exception:
            return

        with self.lock:
            self.status_counts = counts
            self.status_counts_ready = True

    def mark_analysis_started(self) -> None:
        """First tqdm / per-file line: we are past model load; keep stage accurate."""
        with self.lock:
            if self.analyze_phase_started:
                return
            self.analyze_phase_started = True
        self.set_stage("Analyzing photos")

    def set_run_scope(self, scope_type: str, scope_value: str) -> None:
        with self.lock:
            self.run_scope_type = scope_type
            self.run_scope_value = scope_value

    def set_last_paths(
        self,
        *,
        people_root: str | None = None,
        analyze_folder: str | None = None,
        ingest_dest: str | None = None,
    ) -> None:
        with self.lock:
            if people_root is not None:
                self.people_root_last = people_root
            if analyze_folder is not None:
                self.analyze_folder_last = analyze_folder
            if ingest_dest is not None:
                self.ingest_dest_last = ingest_dest

    def set_stage(self, stage: str) -> None:
        with self.lock:
            if self.stage != stage:
                self.stage = stage
                self.activity.append(stage)

    def inc_warning(self) -> None:
        with self.lock:
            self.warnings += 1

    def inc_error(self) -> None:
        with self.lock:
            self.errors += 1

    def set_warning_count(self, count: int) -> None:
        with self.lock:
            self.warnings = max(0, int(count))

    def set_error_count(self, count: int) -> None:
        with self.lock:
            self.errors = max(0, int(count))

    def set_summary(self, key: str, value: int) -> None:
        with self.lock:
            self.summary[key] = value


STATE = AppState()


def _cli_path(name: str) -> str:
    cwd = Path.cwd()
    # Unix editable install; Windows venv uses Scripts\name.exe
    for candidate in (
        cwd / ".venv" / "bin" / name,
        cwd / ".venv" / "Scripts" / f"{name}.exe",
        cwd / ".venv" / "Scripts" / name,
    ):
        if candidate.is_file():
            return str(candidate)
    return name


_INSIGHTFACE_BACKEND = None
_INSIGHTFACE_LOCK = threading.Lock()


def _get_insightface_backend(settings=None):
    """Lazy singleton for Review crop / single-photo reprocess."""
    global _INSIGHTFACE_BACKEND
    from faceit_ai.vision.insightface_backend import InsightFaceBackend

    with _INSIGHTFACE_LOCK:
        if _INSIGHTFACE_BACKEND is None:
            s = settings or load_settings()
            _INSIGHTFACE_BACKEND = InsightFaceBackend(
                s.insightface_root, s.pipeline.insightface
            )
        return _INSIGHTFACE_BACKEND


def _normalize_folder_path(raw: str) -> str:
    """Strip quotes/whitespace from pasted or typed folder paths."""
    text = (raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        text = text[1:-1].strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser())
    except (OSError, ValueError):
        return text


def _folder_picker_client_js() -> str:
    return """
function normalizeFolderPath(s) {
  s = String(s || '').trim();
  if (s.length >= 2 && s.charAt(0) === s.charAt(s.length - 1) && (s.charAt(0) === '"' || s.charAt(0) === "'")) {
    s = s.slice(1, -1).trim();
  }
  return s;
}

async function pickFolder(targetId) {
  try {
    const r = await fetch('/api/pick_folder?target=' + encodeURIComponent(targetId || ''));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    if (!d.ok) {
      if (d.error) alert(d.error);
      return null;
    }
    const el = document.getElementById(targetId);
    const path = normalizeFolderPath(d.path || '');
    if (el) el.value = path;
    try { if (path) localStorage.setItem('faceit_' + targetId, path); } catch (e) {}
    if (typeof syncAnalyzeClearButton === 'function') syncAnalyzeClearButton(targetId);
    if (typeof syncClearButton === 'function') syncClearButton(targetId);
    if (targetId === 'people_root_people' && path) {
      await fetch('/api/set_people_root', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams({people_root: path}).toString()
      });
      location.reload();
      return path;
    }
    return path;
  } catch (e) {
    alert(t('common.alert.picker_failed') + (e && e.message ? ': ' + e.message : ''));
    return null;
  }
}
"""


def _pick_folder_via_osascript(prompt: str) -> str:
    # Escape for AppleScript double-quoted string.
    safe = prompt.replace("\\", "\\\\").replace('"', '\\"')
    script = f'POSIX path of (choose folder with prompt "{safe}")'
    return subprocess.check_output(["osascript", "-e", script], text=True).strip()


def _windows_guid(guid_string: str) -> object:
    """COM GUID struct from '{...}' string (Windows only)."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    guid = GUID()
    hr = ctypes.windll.ole32.CLSIDFromString(wintypes.LPCWSTR(guid_string), ctypes.byref(guid))
    if hr != 0:
        raise OSError(f"CLSIDFromString failed: 0x{hr & 0xFFFFFFFF:08X}")
    return guid


def _windows_com_release(ptr: object) -> None:
    import ctypes

    if not ptr:
        return
    vtable = ctypes.cast(
        ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
        ctypes.POINTER(ctypes.c_void_p * 3),
    ).contents
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    release(ptr)


def _windows_is_cancel(hr: int) -> bool:
    """True for dialog cancel / close (HRESULT may be signed on Windows)."""
    return (hr & 0xFFFFFFFF) == 0x800704C7


def _windows_foreground_owner() -> tuple[object, object]:
    """Create a tiny topmost owner window so the file dialog appears above the browser."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    # Python 3.11 wintypes omits some GDI handle aliases (HCURSOR, HBRUSH, …).
    gdi_handle = getattr(wintypes, "HCURSOR", wintypes.HANDLE)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", getattr(wintypes, "HICON", gdi_handle)),
            ("hCursor", gdi_handle),
            ("hbrBackground", getattr(wintypes, "HBRUSH", gdi_handle)),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class_name = "FaceitAiFolderPickerOwner"
    hinst = kernel32.GetModuleHandleW(None)
    wndproc = ctypes.windll.user32.DefWindowProcW
    wc = WNDCLASSW()
    wc.style = 0
    wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p).value
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = hinst
    wc.hIcon = None
    wc.hCursor = None
    wc.hbrBackground = None
    wc.lpszMenuName = None
    wc.lpszClassName = class_name
    atom = user32.RegisterClassW(ctypes.byref(wc))
    # Ignore "already registered" (error 1410).
    if not atom and ctypes.GetLastError() not in (0, 1410):
        raise OSError(f"RegisterClassW failed: {ctypes.GetLastError()}")

    # WS_EX_TOPMOST | WS_EX_TOOLWINDOW
    ex_style = 0x00000008 | 0x00000080
    # WS_POPUP
    style = 0x80000000
    hwnd = user32.CreateWindowExW(
        ex_style,
        class_name,
        "Faceit AI",
        style,
        0,
        0,
        0,
        0,
        None,
        None,
        hinst,
        None,
    )
    if not hwnd:
        raise OSError(f"CreateWindowExW failed: {ctypes.GetLastError()}")

    # Steal focus from the browser so Show() is owned by a foreground window.
    user32.AllowSetForegroundWindow(-1)
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    cur_thread = kernel32.GetCurrentThreadId()
    if fg_thread and fg_thread != cur_thread:
        user32.AttachThreadInput(cur_thread, fg_thread, True)
    user32.ShowWindow(hwnd, 5)  # SW_SHOW
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    if fg_thread and fg_thread != cur_thread:
        user32.AttachThreadInput(cur_thread, fg_thread, False)
    return hwnd, user32


def _pick_folder_via_windows_dialog(prompt: str, *, mode: str = "dir") -> str:
    """Modern Windows dialog.

    mode=dir  — folder picker (FOS_PICKFOLDERS).
    mode=media — open-file dialog showing images; returns the parent folder so photos
                 are visible while choosing an analyze folder.
    """
    import ctypes
    from ctypes import wintypes

    clsid_file_open = _windows_guid("{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}")
    iid_file_open = _windows_guid("{D57C7288-D4AD-4768-BE02-9D969532D960}")

    fos_pickfolders = 0x00000020
    fos_forcefilesystem = 0x00000040
    fos_nochagedir = 0x00000008
    fos_pathmustexist = 0x00000800
    fos_filemustexist = 0x00001000
    sigdn_filesystem = 0x80058000

    class COMDLG_FILTERSPEC(ctypes.Structure):
        _fields_ = [("pszName", wintypes.LPCWSTR), ("pszSpec", wintypes.LPCWSTR)]

    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    dialog = ctypes.c_void_p()
    owner = None
    user32 = None
    try:
        owner, user32 = _windows_foreground_owner()
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid_file_open),
            None,
            1,  # CLSCTX_INPROC_SERVER
            ctypes.byref(iid_file_open),
            ctypes.byref(dialog),
        )
        if hr != 0 or not dialog:
            raise OSError(f"CoCreateInstance failed: 0x{hr & 0xFFFFFFFF:08X}")

        vtable = ctypes.cast(
            ctypes.cast(dialog, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p * 28),
        ).contents

        get_options = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)
        )(vtable[10])
        set_options = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.c_uint
        )(vtable[9])
        opts = ctypes.c_uint(0)
        get_options(dialog, ctypes.byref(opts))
        if mode == "media":
            set_options(
                dialog,
                (opts.value | fos_forcefilesystem | fos_nochagedir | fos_pathmustexist | fos_filemustexist)
                & ~fos_pickfolders,
            )
            # SetFileTypes — vtable index 4
            filters = (COMDLG_FILTERSPEC * 2)(
                COMDLG_FILTERSPEC(
                    "Photos",
                    "*.jpg;*.jpeg;*.png;*.webp;*.tif;*.tiff;*.heic;*.dng;*.arw;*.cr2;*.cr3;*.nef;*.nrw;*.orf;*.raf;*.rw2;*.pef;*.srw",
                ),
                COMDLG_FILTERSPEC("All files", "*.*"),
            )
            set_file_types = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p
            )(vtable[4])
            set_file_types(dialog, 2, ctypes.cast(filters, ctypes.c_void_p))
        else:
            set_options(
                dialog,
                opts.value | fos_pickfolders | fos_forcefilesystem | fos_nochagedir | fos_pathmustexist,
            )

        if prompt:
            set_title = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, ctypes.c_void_p, wintypes.LPCWSTR
            )(vtable[17])
            set_title(dialog, prompt)

        show = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, wintypes.HWND
        )(vtable[3])
        hr = show(dialog, owner)
        if _windows_is_cancel(hr) or hr != 0:
            raise subprocess.CalledProcessError(1, "windows-folder-picker")

        get_result = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )(vtable[20])
        item = ctypes.c_void_p()
        hr = get_result(dialog, ctypes.byref(item))
        if hr != 0 or not item:
            raise subprocess.CalledProcessError(1, "windows-folder-picker")
        try:
            item_vtable = ctypes.cast(
                ctypes.cast(item, ctypes.POINTER(ctypes.c_void_p))[0],
                ctypes.POINTER(ctypes.c_void_p * 6),
            ).contents
            get_display_name = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.POINTER(wintypes.LPWSTR),
            )(item_vtable[5])
            path_ptr = wintypes.LPWSTR()
            hr = get_display_name(item, sigdn_filesystem, ctypes.byref(path_ptr))
            if hr != 0 or not path_ptr:
                raise subprocess.CalledProcessError(1, "windows-folder-picker")
            path = path_ptr.value
            ole32.CoTaskMemFree(path_ptr)
            if not path:
                raise subprocess.CalledProcessError(1, "windows-folder-picker")
            if mode == "media":
                # Selected a photo → use its containing folder.
                return str(Path(path).expanduser().resolve().parent)
            return str(path)
        finally:
            _windows_com_release(item)
    finally:
        if dialog:
            try:
                _windows_com_release(dialog)
            except Exception:
                pass
        if owner and user32 is not None:
            try:
                user32.DestroyWindow(owner)
            except Exception:
                pass
        try:
            ole32.CoUninitialize()
        except Exception:
            pass


def _pick_folder_via_zenity(prompt: str) -> str:
    return subprocess.check_output(
        ["zenity", "--file-selection", "--directory", "--title", prompt],
        text=True,
    ).strip()


def _pick_folder_via_tk(prompt: str) -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    path = filedialog.askdirectory(title=prompt, mustexist=True)
    root.destroy()
    if not path:
        raise subprocess.CalledProcessError(1, "tkinter")
    return str(path)


_WINDOWS_MEDIA_PICK_TARGETS = frozenset({"review_folder"})


def _pick_folder(*, lang: str = DEFAULT_LANG, target: str = "") -> str:
    """Native folder dialog on the machine running the web server (local UI)."""
    system = platform.system()
    mode = "media" if target in _WINDOWS_MEDIA_PICK_TARGETS else "dir"
    if mode == "media":
        prompt = _t("picker.media_folder", lang)
    else:
        prompt = _t("osascript.pick_folder", lang)
    if system == "Darwin":
        return _normalize_folder_path(
            _pick_folder_via_osascript(
                prompt if mode == "dir" else _t("osascript.pick_folder", lang)
            )
        )
    if system == "Windows":
        try:
            return _normalize_folder_path(_pick_folder_via_windows_dialog(prompt, mode=mode))
        except (OSError, subprocess.CalledProcessError, subprocess.SubprocessError):
            if mode == "dir":
                return _normalize_folder_path(_pick_folder_via_tk(prompt))
            raise
    # Linux / other: prefer zenity, then tkinter.
    try:
        return _normalize_folder_path(_pick_folder_via_zenity(prompt))
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return _normalize_folder_path(_pick_folder_via_tk(prompt))


# analyze_photos now prints per-metric lines, e.g.:
# "this_scan |   files listed in folder: 9"
_RE_SCAN_NEWLY = re.compile(r"newly analyzed[:=]\s*(?P<v>\d+)", re.IGNORECASE)
_RE_SCAN_SKIPPED = re.compile(
    r"skipped\s*\(already in DB\)[:=]\s*(?P<v>\d+)", re.IGNORECASE
)
_RE_SCAN_DECODE = re.compile(r"decode errors[:=]\s*(?P<v>\d+)", re.IGNORECASE)
_RE_SCAN_LISTED = re.compile(r"files listed in folder[:=]\s*(?P<v>\d+)", re.IGNORECASE)

_RE_SYNC_COUNTS = re.compile(
    r"sync_metadata.*synced[:=]\s*(?P<synced>\d+).*no_db_match[:=]\s*(?P<no_db>\d+).*skipped_status[:=]\s*(?P<skipped_status>\d+).*errors[:=]\s*(?P<errors>\d+).*scanned[:=]\s*(?P<scanned>\d+)",
    re.IGNORECASE,
)


def _map_line_to_user_state(line: str) -> None:
    # Drop ANSI color codes (rare when piped, but harmless).
    text = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
    low = text.lower()
    if not text:
        return
    if "loading insightface" in low or "find model:" in low or "applied providers:" in low:
        # InsightFace can emit these lines more than once; never override real analysis progress.
        if not bool(STATE.snapshot().get("analyze_phase_started")):
            STATE.set_stage("Loading models")
        return
    if "copying to archive" in low or "folder_ingest" in low:
        STATE.set_stage("Copying to archive")
        return
    if "starting per-file check" in low:
        STATE.mark_analysis_started()
        return
    if "querying cumulative counts" in low:
        STATE.set_stage("Finishing analysis")
        STATE.refresh_status_counts_final()
        return
    if "stopped early" in low:
        STATE.add_activity("Stopped early — running checkout (flagged export / people collect)…")
        return
    if "running flagged export" in low:
        STATE.set_stage("Exporting flagged photos")
        return
    if "collecting strong matches" in low:
        STATE.set_stage("Collecting to people folders")
        return
    if "sync_metadata" in low and "done:" in low:
        STATE.set_stage("Writing metadata")

    # Summary counters: parse even if finish_run already flipped running=False (Windows flush race).
    m_listed = _RE_SCAN_LISTED.search(text)
    if m_listed:
        n = int(m_listed.group("v"))
        STATE.set_summary("files_found", n)
        STATE.add_activity(f"Found {n} photos in folder.")
    m_new = _RE_SCAN_NEWLY.search(text)
    if m_new:
        STATE.set_summary("newly_analyzed", int(m_new.group("v")))
    m_skip = _RE_SCAN_SKIPPED.search(text)
    if m_skip:
        STATE.set_summary("already_skipped", int(m_skip.group("v")))
    m_decode = _RE_SCAN_DECODE.search(text)
    if m_decode:
        decode_v = int(m_decode.group("v"))
        STATE.set_summary("decode_errors", decode_v)
        STATE.set_warning_count(decode_v)
        # Single, user-friendly activity line for unreadable files.
        STATE.add_activity(
            f"Analysis finished: {STATE.summary.get('newly_analyzed', '-')} analyzed. "
            f"Unreadable skipped: {decode_v}."
        )
    m2 = _RE_SYNC_COUNTS.search(text)
    if m2:
        STATE.set_summary("metadata_synced", int(m2.group("synced")))
        STATE.set_summary("metadata_no_db_match", int(m2.group("no_db")))
        STATE.set_summary("metadata_skipped_status", int(m2.group("skipped_status")))
        STATE.set_summary("metadata_errors", int(m2.group("errors")))
        STATE.set_error_count(int(m2.group("errors")))
        STATE.add_activity(
            f"Metadata sync finished. Updated: {m2.group('synced')}, skipped (no DB match / wrong status): {int(m2.group('no_db')) + int(m2.group('skipped_status'))}, errors: {m2.group('errors')}."
        )


_RE_TQDM_PROGRESS = re.compile(
    # tqdm Photos bar (rate may be "?file/s", "it/s", or missing while buffered).
    r"(?:Photos:\s*)?\d{1,3}%\|",
    re.IGNORECASE,
)
# e.g. Photos:  52%|█████▏    | 13/25 [00:09<00:09,  1.20file/s, DJI_0178.DNG]
_RE_TQDM_COUNTS = re.compile(
    r"(?P<prefix>.*?)(?P<pct>\d{1,3})%\|(?P<bar>[^|]*)\|\s*(?P<cur>\d+)/(?P<tot>\d+)(?P<rest>\s*\[.*)?$"
)


def _is_tqdm_progress_line(line: str) -> bool:
    return bool(_RE_TQDM_PROGRESS.search(line))


def _finalize_progress_line(line: str, *, success: bool) -> str:
    """Rewrite the last tqdm line to 100% (or Failed) so the Progress box matches finish."""
    text = (line or "").strip()
    if not success:
        if text and _RE_TQDM_COUNTS.search(text):
            m = _RE_TQDM_COUNTS.search(text)
            assert m is not None
            tot = m.group("tot")
            prefix = m.group("prefix")
            rest = m.group("rest") or ""
            return f"{prefix}100%|{'█' * 10}| {tot}/{tot}{rest} — failed"
        return "Failed."
    if not text:
        return "Finished."
    m = _RE_TQDM_COUNTS.search(text)
    if m is None:
        return text if text.endswith("finished") or text == "Finished." else f"{text} — finished"
    tot = m.group("tot")
    prefix = m.group("prefix")
    rest = m.group("rest") or ""
    return f"{prefix}100%|{'█' * 10}| {tot}/{tot}{rest}"


def _cli_env() -> dict[str, str]:
    """Env for CLI subprocesses — unbuffered UTF-8 so Progress/logs survive Windows cp1252."""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Python 3.7+ on Windows: make stdout/stderr UTF-8 instead of the ANSI code page.
    env["PYTHONUTF8"] = "1"
    return env


def _popen_cli(cmd: list[str], *, start_new_session: bool = False) -> subprocess.Popen[str]:
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_cli_env(),
        start_new_session=start_new_session,
    )


def _kill_process_tree(proc: subprocess.Popen[str], *, force: bool = False) -> None:
    """Signal a CLI subprocess (and its group on Unix). Windows has no killpg."""
    if not force:
        if hasattr(os, "killpg") and hasattr(os, "getpgid") and proc.pid:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                return
            except (AttributeError, ProcessLookupError, PermissionError, OSError):
                pass
        proc.terminate()
        return

    if hasattr(os, "killpg") and hasattr(os, "getpgid") and proc.pid and hasattr(signal, "SIGKILL"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (AttributeError, ProcessLookupError, PermissionError, OSError):
            pass
    proc.kill()


def _send_stop_signal(proc: subprocess.Popen[str] | None) -> None:
    """Ask a subprocess to stop (SIGTERM / terminate). Does not block."""
    if proc is None or proc.poll() is not None:
        return
    try:
        _kill_process_tree(proc, force=False)
    except (OSError, subprocess.SubprocessError) as e:
        STATE.add_log(f"[warn] stop signal failed: {e}")


def _terminate_proc(proc: subprocess.Popen[str] | None, *, graceful: bool = False) -> None:
    """Terminate a tracked subprocess (and its process group when possible)."""
    if proc is None or proc.poll() is not None:
        return
    timeout = 120 if graceful else 3
    try:
        _kill_process_tree(proc, force=False)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc, force=True)
            proc.wait(timeout=2)
    except (OSError, subprocess.SubprocessError) as e:
        STATE.add_log(f"[warn] stop failed: {e}")


def _release_own_folder_claim(folder: str, *, message: str) -> int:
    """Release this machine's open DB claim for ``folder`` (0 if none / invalid)."""
    folder = (folder or "").strip()
    if not folder:
        return 0
    try:
        settings = load_settings()
        _, session_factory = create_engine_and_session_factory(settings.database_url)
        return release_folder_claims(
            session_factory,
            folder,
            host=this_host(),
            status="cancelled",
            message=message,
        )
    except Exception as e:
        STATE.add_log(f"[warn] could not release folder claim: {e}")
        return 0


def _stop_run(*, lang: str = DEFAULT_LANG) -> dict[str, object]:
    if not STATE.request_stop():
        return {"ok": False, "error": _t("api.no_run", lang)}
    STATE.add_log("[warn] stop requested by user")
    STATE.add_activity(
        "Stop requested — finishing current file, then checkout (flagged / people folders)…"
    )
    return {"ok": True}


def _prepare_analyze_run(folder: str) -> None:
    """Reset Current Status synchronously before redirect (avoids stale stats flash)."""
    folder = (folder or "").strip()
    STATE.reset_for_run("Analyze")
    if folder:
        STATE.set_run_scope("folder", folder)
        try:
            settings = load_settings()
            img = settings.pipeline.image
            n_files = len(
                list_scannable_image_paths(
                    Path(folder),
                    extensions=img.scan_extensions(),
                    ignore_filename_substrings=img.ignore_filename_substrings,
                    exclude_flagged_subtree=True,
                )
            )
            STATE.set_summary("files_found", n_files)
            STATE.add_activity(f"Found {n_files} photos in folder.")
        except Exception:
            pass
    STATE.add_log("=== Analyze ===")


def _run_commands(title: str, commands: list[list[str]], *, skip_reset: bool = False) -> None:
    if not skip_reset:
        if STATE.snapshot()["running"]:
            STATE.add_log("[warn] job already running")
            return
        STATE.reset_for_run(title)
        # reset_for_run clears run_scope; restore folder scope for analyze so Blocked/Review/OK can update.
        if commands and len(commands[0]) > 1 and "analyze_photos" in commands[0][0]:
            STATE.set_run_scope("folder", commands[0][1])
        STATE.add_log(f"=== {title} ===")
    elif not STATE.snapshot()["running"]:
        STATE.add_log("[warn] analyze prepare expected running state")
        return

    def _worker() -> None:
        try:
            success = True
            for cmd in commands:
                with STATE.lock:
                    if STATE.stop_requested:
                        success = False
                        break
                STATE.add_log("$ " + " ".join(cmd))
                is_analyze = "analyze_photos" in cmd[0]
                if "register_person" in cmd[0]:
                    STATE.set_stage("Registering new people")
                    STATE.add_activity("Registering people from selected folder...")
                elif is_analyze:
                    folder = cmd[1] if len(cmd) > 1 else ""
                    if not skip_reset:
                        STATE.set_stage("Scanning folder")
                        STATE.set_progress_line("")
                        if folder:
                            try:
                                settings = load_settings()
                                img = settings.pipeline.image
                                n_files = len(
                                    list_scannable_image_paths(
                                        Path(folder),
                                        extensions=img.scan_extensions(),
                                        ignore_filename_substrings=img.ignore_filename_substrings,
                                        exclude_flagged_subtree=True,
                                    )
                                )
                                STATE.set_summary("files_found", n_files)
                                STATE.add_activity(f"Found {n_files} photos in folder.")
                            except Exception:
                                pass
                        STATE.set_stage("Preparing")
                        STATE.add_activity("Preparing analysis run...")
                    else:
                        STATE.set_stage("Preparing")
                        STATE.add_activity("Preparing analysis run...")
                        STATE.set_progress_line("")
                proc = _popen_cli(cmd, start_new_session=True)
                STATE.set_current_proc(proc)
                assert proc.stdout is not None
                for line in proc.stdout:
                    t = line.rstrip("\n")
                    # Progress panel is analyze-only; register/other tqdm stays out of it.
                    if _is_tqdm_progress_line(t):
                        if is_analyze:
                            STATE.set_progress_line(t)
                            STATE.mark_analysis_started()
                            STATE.maybe_refresh_live_status_counts()
                        continue
                    STATE.add_log(t)
                    _map_line_to_user_state(t)
                rc = proc.wait()
                STATE.set_current_proc(None)
                with STATE.lock:
                    stopped = STATE.stop_requested
                if stopped:
                    if rc == 0:
                        STATE.add_log("[info] stopped early; checkout completed")
                        STATE.add_activity(
                            "Stopped early — checkout finished for photos processed so far."
                        )
                    else:
                        STATE.add_log("[warn] command stopped by user")
                        success = False
                        break
                if rc != 0:
                    STATE.add_log(f"[error] command failed with exit code {rc}")
                    STATE.inc_error()
                    success = False
                    break
            STATE.add_log("=== done ===")
            STATE.finish_run(success)
        except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover
            STATE.add_log(f"[error] {e}")
            STATE.inc_error()
            STATE.finish_run(False)

    threading.Thread(target=_worker, daemon=True).start()


def _soft_deactivate_person(name: str) -> None:
    """Mark person inactive (keep embeddings) and re-label affected photos as publishable."""
    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    with session_scope(session_factory) as session:
        person = session.scalar(
            select(Person).where(Person.name == name, Person.active.is_(True))
        )
        if person is None:
            return
        person.active = False
    metadata_sync = build_metadata_sync(
        settings,
        log=logging.getLogger("faceit_ai.metadata"),
        audit=logging.getLogger("faceit_ai.audit"),
    )
    run_redecide_and_sync_person(
        person_name=name,
        consent_allowed=False,
        settings=settings,
        session_factory=session_factory,
        metadata=metadata_sync,
        audit=logging.getLogger("faceit_ai.audit"),
    )


def _reactivate_person(name: str) -> None:
    """Re-enable a soft-deactivated person (embeddings already present)."""
    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    with session_scope(session_factory) as session:
        person = session.scalar(
            select(Person)
            .where(Person.name == name, Person.active.is_(False))
            .order_by(Person.id.desc())
        )
        if person is None:
            return
        person.active = True
    # Keep prior consent; just re-apply decisions with current consent flags.
    with session_scope(session_factory) as session:
        consent = session.scalar(
            select(Consent)
            .join(Person, Consent.person_id == Person.id)
            .where(Person.name == name, Person.active.is_(True))
        )
        allowed = bool(consent.consent_given) if consent is not None else False
    metadata_sync = build_metadata_sync(
        settings,
        log=logging.getLogger("faceit_ai.metadata"),
        audit=logging.getLogger("faceit_ai.audit"),
    )
    run_redecide_and_sync_person(
        person_name=name,
        consent_allowed=allowed,
        settings=settings,
        session_factory=session_factory,
        metadata=metadata_sync,
        audit=logging.getLogger("faceit_ai.audit"),
    )


def _reregister_person(name: str, *, lang: str = DEFAULT_LANG) -> dict[str, object]:
    """Wipe embeddings for one person, keep consent, then re-scan only their folder."""
    name = name.strip()
    if not name:
        return {"ok": False, "error": _t("api.missing_person", lang)}
    if bool(STATE.snapshot()["running"]):
        return {"ok": False, "error": _t("api.job_running", lang)}

    person_dir = _safe_person_dir(name)
    if person_dir is None:
        return {"ok": False, "error": _t("api.no_folder_for_person", lang, name=name)}
    total, _ = _count_photos_in_person_folder(person_dir)
    if total == 0:
        return {
            "ok": False,
            "error": _t("api.person_no_images", lang, name=name),
        }

    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    with session_scope(session_factory) as session:
        person = session.scalar(select(Person).where(Person.name == name))
        if person is None:
            # Not in DB yet — just register.
            person_id = None
        else:
            person_id = int(person.id)
            person.active = True
            session.execute(
                FaceEmbedding.__table__.delete().where(FaceEmbedding.person_id == person_id)
            )

    def _worker() -> None:
        try:
            STATE.reset_for_run(f"Re-register {name}")
            STATE.set_stage(f"Re-registering {name}")
            STATE.add_activity(
                f"Wiped embeddings for {name}; scanning folder photos only…"
                if person_id is not None
                else f"Registering {name} from folder…"
            )
            cmd = [
                _cli_path("register_person"),
                str(person_dir),
                "--name",
                name,
                "--no-consent",
            ]
            STATE.add_log("$ " + " ".join(cmd))
            proc = _popen_cli(cmd)
            assert proc.stdout is not None
            for line in proc.stdout:
                t = line.rstrip("\n")
                # Keep Analyze Progress panel clean — register tqdm is not shown there.
                if _is_tqdm_progress_line(t):
                    continue
                STATE.add_log(t)
            rc = proc.wait()
            if rc != 0:
                STATE.add_log(f"[error] re-register failed for {name} (exit {rc})")
                STATE.inc_error()
                STATE.add_activity(f"Re-register failed for {name}.")
                STATE.finish_run(False)
                return
            STATE.add_activity(f"Re-registered {name} from their folder.")
            STATE.finish_run(True)
        except (OSError, subprocess.SubprocessError) as e:
            STATE.add_log(f"[error] {e}")
            STATE.inc_error()
            STATE.finish_run(False)

    threading.Thread(target=_worker, daemon=True).start()
    return {
        "ok": True,
        "syncing": True,
        "message": _t("api.reregister_msg", lang, name=name),
    }


def _reregister_all_people(*, lang: str = DEFAULT_LANG) -> dict[str, object]:
    """Wipe embeddings and re-run register_person for every person folder with photos."""
    if bool(STATE.snapshot()["running"]):
        return {"ok": False, "error": _t("api.job_running", lang)}
    if not _resolved_people_root():
        return {"ok": False, "error": _t("api.choose_people_folder", lang)}

    targets: list[tuple[str, Path]] = []
    for row in _list_people_rows():
        name = str(row.get("name") or "").strip()
        if not name or int(row.get("photos") or 0) <= 0:
            continue
        person_dir = _safe_person_dir(name)
        if person_dir is None:
            continue
        targets.append((name, person_dir))

    if not targets:
        return {"ok": False, "error": _t("api.reregister_all_none", lang)}

    def _worker() -> None:
        try:
            STATE.reset_for_run("Re-register all")
            STATE.add_activity(f"Re-registering {len(targets)} people from their folders…")
            failed: list[str] = []
            settings = load_settings()
            _, session_factory = create_engine_and_session_factory(settings.database_url)
            for name, person_dir in targets:
                with STATE.lock:
                    if STATE.stop_requested:
                        break
                STATE.set_stage(f"Re-registering {name}")
                STATE.add_activity(f"Re-registering {name}…")
                with session_scope(session_factory) as session:
                    person = session.scalar(select(Person).where(Person.name == name))
                    if person is not None:
                        person.active = True
                        session.execute(
                            FaceEmbedding.__table__.delete().where(
                                FaceEmbedding.person_id == int(person.id)
                            )
                        )
                cmd = [
                    _cli_path("register_person"),
                    str(person_dir),
                    "--name",
                    name,
                    "--no-consent",
                ]
                STATE.add_log("$ " + " ".join(cmd))
                proc = _popen_cli(cmd, start_new_session=True)
                STATE.set_current_proc(proc)
                assert proc.stdout is not None
                for line in proc.stdout:
                    t = line.rstrip("\n")
                    if _is_tqdm_progress_line(t):
                        continue
                    STATE.add_log(t)
                rc = proc.wait()
                STATE.set_current_proc(None)
                with STATE.lock:
                    stopped = STATE.stop_requested
                if stopped:
                    STATE.add_log("[warn] re-register all stopped by user")
                    failed.append(name)
                    break
                if rc != 0:
                    STATE.add_log(f"[error] re-register failed for {name} (exit {rc})")
                    STATE.inc_error()
                    STATE.add_activity(f"Re-register failed for {name}.")
                    failed.append(name)
                    continue
                STATE.add_activity(f"Re-registered {name}.")
            if failed:
                STATE.add_activity(
                    f"Re-register all finished with {len(failed)} failure(s)."
                )
                STATE.finish_run(False)
            else:
                STATE.add_activity(f"Re-registered all {len(targets)} people.")
                STATE.finish_run(True)
        except (OSError, subprocess.SubprocessError) as e:
            STATE.add_log(f"[error] {e}")
            STATE.inc_error()
            STATE.finish_run(False)

    threading.Thread(target=_worker, daemon=True).start()
    return {
        "ok": True,
        "syncing": True,
        "count": len(targets),
        "message": _t("api.reregister_all_msg", lang, count=len(targets)),
    }


def _scan_people_root(root_text: str) -> None:
    """Non-AJAX fallback: run the same folder sync."""
    result = _scan_people_plan_and_start(root_text)
    if not result.get("ok"):
        STATE.add_log(f"[error] {result.get('error')}")


def _scan_people_plan_and_start(root_text: str, *, lang: str = DEFAULT_LANG) -> dict[str, object]:
    """Full people-folder sync: add missing, soft-remove extras, leave unchanged.

    Returns JSON for the People page inline (green/red) result; stays on the page.
    """
    if not root_text.strip():
        return {"ok": False, "error": _t("api.choose_people_folder", lang)}
    root = Path(root_text).expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "error": _t("api.invalid_people_folder", lang, path=root)}
    if bool(STATE.snapshot()["running"]):
        return {"ok": False, "error": _t("api.job_running", lang)}

    STATE.set_last_paths(people_root=str(root))
    _persist_people_dir(str(root))
    _invalidate_people_photo_counts()

    folder_names = set(_people_folder_names(root))
    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)

    with session_scope(session_factory) as session:
        for name in folder_names:
            sync_person_profile_to_db(session, folder_name=name, folder=root / name)

    active_names: set[str] = set()
    inactive_names: set[str] = set()
    with session_scope(session_factory) as session:
        for name, active in session.execute(select(Person.name, Person.active)).all():
            n = str(name)
            if active:
                active_names.add(n)
            else:
                inactive_names.add(n)

    to_remove = sorted(active_names - folder_names)
    missing_in_db = folder_names - active_names
    to_reactivate = sorted(n for n in missing_in_db if n in inactive_names)
    candidates = sorted(n for n in missing_in_db if n not in inactive_names)
    unchanged = sorted(folder_names & active_names)

    # Skip empty person folders up front (register_person requires at least one image).
    to_register: list[str] = []
    empty_folders: list[str] = []
    for name in candidates:
        total, _ = _count_photos_in_person_folder(root / name)
        if total == 0:
            empty_folders.append(name)
        else:
            to_register.append(name)

    summary_msg = (
        f"To add: {len(to_register)}, empty (skipped): {len(empty_folders)}, "
        f"reactivate: {len(to_reactivate)}, remove: {len(to_remove)}, "
        f"unchanged: {len(unchanged)}."
    )
    if empty_folders:
        summary_msg += (
            " Empty folders need photos first: " + ", ".join(empty_folders) + "."
        )

    if not to_register and not to_reactivate and not to_remove:
        STATE.add_log(summary_msg)
        if empty_folders:
            return {
                "ok": False,
                "registering": False,
                "syncing": False,
                "error": _t("api.scan_empty_error", lang, summary=summary_msg),
            }
        return {
            "ok": True,
            "registering": False,
            "syncing": False,
            "message": _t("api.scan_matches", lang, summary=summary_msg),
        }

    def _worker() -> None:
        registered = 0
        failed: list[str] = []
        try:
            STATE.reset_for_run("Sync people folder")
            STATE.set_stage("Syncing people with folder")
            STATE.add_activity(summary_msg)
            for name in empty_folders:
                STATE.add_activity(
                    f"Skipped {name}: no supported images in folder (add photos, then re-scan)."
                )
                STATE.add_log(f"[skip] empty person folder: {name}")

            for name in to_remove:
                STATE.set_stage(f"Removing {name} (soft)")
                STATE.add_activity(
                    f"Soft-removing {name}: stop matching, keep embeddings, re-label photos…"
                )
                try:
                    _soft_deactivate_person(name)
                    STATE.add_log(f"[removed] {name} (embeddings kept)")
                except Exception as e:
                    STATE.add_log(f"[error] soft-remove {name}: {e}")
                    STATE.inc_error()
                    failed.append(name)

            for name in to_reactivate:
                STATE.set_stage(f"Reactivating {name}")
                STATE.add_activity(f"Reactivating {name} (embeddings already stored)…")
                try:
                    _reactivate_person(name)
                    STATE.add_log(f"[reactivated] {name}")
                except Exception as e:
                    STATE.add_log(f"[error] reactivate {name}: {e}")
                    STATE.inc_error()
                    failed.append(name)

            for name in to_register:
                person_dir = root / name
                cmd = [
                    _cli_path("register_person"),
                    str(person_dir),
                    "--name",
                    name,
                    "--no-consent",
                ]
                STATE.set_stage(f"Registering {name}")
                STATE.add_activity(f"Registering {name} from folder…")
                STATE.add_log("$ " + " ".join(cmd))
                proc = _popen_cli(cmd)
                assert proc.stdout is not None
                for line in proc.stdout:
                    t = line.rstrip("\n")
                    # Keep Analyze Progress panel clean — register tqdm is not shown there.
                    if _is_tqdm_progress_line(t):
                        continue
                    STATE.add_log(t)
                rc = proc.wait()
                if rc != 0:
                    STATE.add_log(f"[error] register_person failed for {name} (exit {rc})")
                    STATE.add_activity(
                        f"Failed to register {name}. Check Technical Log for details."
                    )
                    STATE.inc_error()
                    failed.append(name)
                    continue
                registered += 1
                STATE.add_activity(f"Registered {name}.")

            final = (
                f"Done: registered {registered}/{len(to_register)}, "
                f"skipped empty {len(empty_folders)}"
                + (f", failed: {', '.join(failed)}" if failed else "")
                + "."
            )
            STATE.add_log("=== people sync done ===")
            STATE.add_log(final)
            STATE.add_activity(final)
            STATE.finish_run(len(failed) == 0)
        except (OSError, subprocess.SubprocessError) as e:
            STATE.add_log(f"[error] {e}")
            STATE.inc_error()
            STATE.finish_run(False)

    threading.Thread(target=_worker, daemon=True).start()
    return {
        "ok": True,
        "registering": True,
        "syncing": True,
        "added": len(to_register),
        "empty": len(empty_folders),
        "reactivated": len(to_reactivate),
        "removed": len(to_remove),
        "unchanged": len(unchanged),
        "message": _t("api.scan_syncing", lang, summary=summary_msg),
    }


def _compute_outcome_counts(scope_type: str, scope_value: str) -> dict[str, int]:
    """Count AssetDecision outcomes in the DB, scoped to the run.

    - folder: counts decisions for assets under the folder (Mac/Windows/UNC path-safe)
    - person: counts decisions for assets that have stored faces matched to this person
    """
    from sqlalchemy import or_

    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    out = {"blocked": 0, "review": 0, "ok": 0}

    with session_scope(session_factory) as session:
        if scope_type == "folder":
            folder = (scope_value or "").strip()
            if not folder:
                return out

            base = select(Asset.path, AssetDecision.status).join(
                Asset, AssetDecision.asset_id == Asset.id
            )

            def _count_rows(candidates: list[tuple[str, str]]) -> int:
                n = 0
                for path_str, st in candidates:
                    if not asset_path_in_folder(path_str, folder):
                        continue
                    key = st.lower()
                    if key in out:
                        out[key] += 1
                        n += 1
                return n

            prefixes = folder_path_prefixes(folder)
            matched = 0
            if prefixes:
                stmt = base.where(or_(*[Asset.path.startswith(p) for p in prefixes]))
                matched = _count_rows(
                    [(str(p), str(st)) for p, st in session.execute(stmt).all()]
                )
            # Cross-OS shared DB: Windows folder vs Mac-stored paths → SQL miss.
            if matched == 0:
                out = {"blocked": 0, "review": 0, "ok": 0}
                _count_rows([(str(p), str(st)) for p, st in session.execute(base).all()])
            return out

        if scope_type == "person":
            name = scope_value.strip()
            person_id = session.scalar(
                select(Person.id).where(Person.name == name, Person.active.is_(True))
            )
            if person_id is None:
                return out

            stmt = (
                select(
                    AssetDecision.status,
                    func.count(func.distinct(AssetDecision.asset_id)),
                )
                .join(Asset, AssetDecision.asset_id == Asset.id)
                .join(AssetFace, AssetFace.asset_id == Asset.id)
                .where(AssetFace.match_person_id == int(person_id))
                .group_by(AssetDecision.status)
            )
            rows = session.execute(stmt).all()
            for st, cnt in rows:
                key = str(st).lower()
                if key in out:
                    out[key] = int(cnt or 0)
            return out

    return out


def _resolved_people_root() -> Path | None:
    text = (STATE.people_root_last or "").strip() or _load_people_dir_from_config()
    if not text:
        return None
    root = Path(text).expanduser().resolve()
    return root if root.is_dir() else None


def _person_needs_reregister(photos: int, embeddings: int) -> bool:
    """True when folder photos and stored embeddings differ (person needs Re-register)."""
    return photos > 0 and photos != embeddings


def _mismatch_tooltip(photos: int, embeddings: int, lang: str = DEFAULT_LANG) -> str:
    return _t("people.mismatch.tip", lang, n=photos, m=embeddings)


def _help_mark(tip: str) -> str:
    """Small (?) marker with rich hover tip (data-tip)."""
    return f'<span class="col-help" data-tip="{html.escape(tip, quote=True)}"> (?)</span>'


def _label_with_help(text: str, tip: str = "") -> str:
    """Checkbox/form label text with (?) kept inline when text wraps."""
    inner = html.escape(text)
    if tip:
        inner += _help_mark(tip)
    return f'<span class="label-with-help">{inner}</span>'


def _settings_ai_intro_html(lang: str) -> str:
    """Short bilingual overview for AI Model settings."""
    tune_items = "".join(
        f"<li>{html.escape(_t(k, lang))}</li>"
        for k in (
            "settings.ai.tune_1",
            "settings.ai.tune_2",
            "settings.ai.tune_3",
            "settings.ai.tune_4",
        )
    )
    return (
        f'<div class="settings-ai-intro">'
        f"<p>{html.escape(_t('settings.ai.intro_pipeline', lang))}</p>"
        f"<p>{html.escape(_t('settings.ai.intro_together', lang))}</p>"
        f"<p><strong>{html.escape(_t('settings.ai.tune_title', lang))}</strong></p>"
        f"<ul>{tune_items}</ul>"
        f"<p>{html.escape(_t('settings.ai.tune_miss', lang))}</p>"
        f"</div>"
    )


def _people_labels_map(root: Path | None = None) -> dict[str, str]:
    """Folder slug -> display name for UI."""
    if root is None:
        root = _resolved_people_root()
    if root is None:
        return {}
    out: dict[str, str] = {}
    for name in _people_folder_names(root):
        profile = profile_for_folder(root / name, name)
        out[name] = profile.display_name or display_name_from_slug(name)
    return out


def _list_people_for_review() -> list[dict[str, str]]:
    labels = _people_labels_map()
    return [{"slug": s, "display_name": labels.get(s, s)} for s in sorted(labels)]


def _collect_all_people_tags(rows: list[dict[str, object]]) -> list[str]:
    tags: set[str] = set()
    for r in rows:
        for t in r.get("tags") or []:
            if isinstance(t, dict):
                label = str(t.get("tag") or "").strip()
            else:
                label = str(t).strip()
            if label:
                tags.add(label)
    return sorted(tags)


def _save_uploaded_person_photos(folder: Path, uploads: list[object]) -> int:
    settings = load_settings()
    exts = set(settings.pipeline.image.scan_extensions())
    folder.mkdir(parents=True, exist_ok=True)
    saved = 0
    for item in uploads:
        filename = getattr(item, "filename", None)
        if not filename or not str(filename).strip():
            continue
        raw = getattr(item, "file", None)
        if raw is None:
            continue
        safe_name = Path(str(filename)).name
        if safe_name.startswith(".") or ".." in safe_name:
            continue
        ext = Path(safe_name).suffix.lower()
        if ext not in exts:
            continue
        dest = folder / safe_name
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            n = 2
            while dest.exists():
                dest = folder / f"{stem}_{n}{suffix}"
                n += 1
        data = raw.read()
        if not data:
            continue
        dest.write_bytes(data)
        saved += 1
    return saved


def _multipart_uploads(fs: MultipartForm, field: str) -> list[object]:
    if field not in fs:
        return []
    raw = fs[field]
    if isinstance(raw, list):
        return [x for x in raw if getattr(x, "filename", None)]
    if getattr(raw, "filename", None):
        return [raw]
    return []


def _ensure_person_folder_request(
    name: str,
    display_name: str = "",
    *,
    lang: str = DEFAULT_LANG,
) -> dict[str, object]:
    """Create people_root/<slug>/ + person.json if missing (Review: add detected person)."""
    root = _resolved_people_root()
    if root is None:
        return {"ok": False, "error": _t("api.people_not_configured", lang)}
    slug = (name or "").strip()
    if not slug or slug in (".", "..") or "/" in slug or "\\" in slug:
        return {"ok": False, "error": _t("api.invalid_person_name", lang, name=name)}

    existing = existing_person_folder(root, slug)
    if existing is not None:
        folder = root / existing
        profile = profile_for_folder(folder, existing)
        return {
            "ok": True,
            "slug": existing,
            "display_name": profile.display_name or display_name_from_slug(existing),
            "created": False,
        }

    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    first = ""
    last = ""
    display = (display_name or "").strip()
    with session_scope(session_factory) as session:
        person = session.scalar(select(Person).where(Person.name == slug))
        if person is not None:
            first = (person.first_name or "").strip()
            last = (person.last_name or "").strip()
            if not display:
                display = (person.display_name or "").strip()

    if not first and not last and "_" in slug:
        # Legacy slug nachname_vorname
        last_part, _, first_part = slug.partition("_")
        last = last_part.replace("-", " ").strip()
        first = first_part.replace("-", " ").strip()
    if not display:
        display = default_display_name(first, last) or display_name_from_slug(slug)

    folder = root / slug
    profile = PersonProfile(
        first_name=first,
        last_name=last,
        display_name=display,
        tags=[],
    )
    write_person_json(folder, profile)
    with session_scope(session_factory) as session:
        repo = ConsentRepository(session)
        person = repo.upsert_person_with_consent(
            name=slug,
            consent_given=False,
            usage_social=True,
            usage_web=True,
            usage_internal=True,
            usage_print=True,
        )
        apply_profile_to_person(person, profile)
        sync_person_profile_to_db(session, folder_name=slug, folder=folder)
    return {
        "ok": True,
        "slug": slug,
        "display_name": profile.display_name,
        "created": True,
        "message": _t("api.ensure_folder_ok", lang, display=profile.display_name, slug=slug),
    }


def _create_person_request(fs: MultipartForm, *, lang: str = DEFAULT_LANG) -> dict[str, object]:
    root = _resolved_people_root()
    if root is None:
        return {"ok": False, "error": _t("api.people_not_configured", lang)}
    first = (fs.getfirst("first_name") or "").strip()
    last = (fs.getfirst("last_name") or "").strip()
    display_override = (fs.getfirst("display_name") or "").strip()
    consent_raw = (fs.getfirst("consent") or "blocked").strip().lower()
    consent_given = consent_raw == "allowed"
    if not first or not last:
        return {"ok": False, "error": _t("api.create_need_names", lang)}
    try:
        slug = folder_slug(last, first)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    existing = existing_person_folder(root, slug)
    if existing is not None:
        return {
            "ok": False,
            "error": _t("api.create_exists", lang, path=existing),
        }
    folder = root / slug
    profile = PersonProfile(
        first_name=first,
        last_name=last,
        display_name=display_override or default_display_name(first, last),
        tags=[],
    )
    write_person_json(folder, profile)
    n_photos = _save_uploaded_person_photos(folder, _multipart_uploads(fs, "photos"))
    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    with session_scope(session_factory) as session:
        repo = ConsentRepository(session)
        person = repo.upsert_person_with_consent(
            name=slug,
            consent_given=consent_given,
            usage_social=True,
            usage_web=True,
            usage_internal=True,
            usage_print=True,
        )
        apply_profile_to_person(person, profile)
        sync_person_profile_to_db(session, folder_name=slug, folder=folder)
    return {
        "ok": True,
        "message": _t("api.create_ok", lang, display=profile.display_name, slug=slug, n=n_photos),
        "slug": slug,
        "display_name": profile.display_name,
    }


def _update_person_request(fs: MultipartForm, *, lang: str = DEFAULT_LANG) -> dict[str, object]:
    root = _resolved_people_root()
    if root is None:
        return {"ok": False, "error": _t("api.people_not_configured", lang)}
    slug = (fs.getfirst("name") or fs.getfirst("slug") or "").strip()
    if not slug:
        return {"ok": False, "error": _t("api.missing_person", lang)}
    folder = root / slug
    if not folder.is_dir():
        return {"ok": False, "error": _t("api.person_folder_missing", lang, slug=slug)}
    profile = profile_for_folder(folder, slug)
    first = (fs.getfirst("first_name") or profile.first_name).strip()
    last = (fs.getfirst("last_name") or profile.last_name).strip()
    display_override = (fs.getfirst("display_name") or "").strip()
    consent_raw = (fs.getfirst("consent") or "").strip().lower()
    if first:
        profile.first_name = first
    if last:
        profile.last_name = last
    if display_override:
        profile.display_name = display_override
    else:
        profile.display_name = default_display_name(profile.first_name, profile.last_name)
    write_person_json(folder, profile)
    n_photos = _save_uploaded_person_photos(folder, _multipart_uploads(fs, "photos"))
    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    with session_scope(session_factory) as session:
        repo = ConsentRepository(session)
        person = session.scalar(select(Person).where(Person.name == slug))
        if person is None:
            person = repo.upsert_person_with_consent(
                name=slug,
                consent_given=(consent_raw == "allowed") if consent_raw else False,
                usage_social=True,
                usage_web=True,
                usage_internal=True,
                usage_print=True,
            )
        apply_profile_to_person(person, profile)
        if consent_raw in ("allowed", "blocked"):
            repo.update_consent_for_person_name(name=slug, consent_given=(consent_raw == "allowed"))
        sync_person_profile_to_db(session, folder_name=slug, folder=folder)
    msg = _t("api.update_ok", lang, display=profile.display_name)
    if n_photos:
        msg += f" Added {n_photos} photo(s)."
    return {"ok": True, "message": msg, "slug": slug}


def _update_person_tags_request(form: dict[str, str], *, lang: str = DEFAULT_LANG) -> dict[str, object]:
    root = _resolved_people_root()
    if root is None:
        return {"ok": False, "error": _t("api.people_not_configured", lang)}
    slug = form.get("name", "").strip()
    if not slug:
        return {"ok": False, "error": _t("api.missing_person", lang)}
    folder = root / slug
    if not folder.is_dir():
        return {"ok": False, "error": _t("api.person_folder_missing", lang, slug=slug)}
    profile = profile_for_folder(folder, slug)
    cycle = form.get("cycle", "").strip()
    if cycle:
        try:
            cycle_tag_consent(profile, cycle)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
    else:
        add = [t.strip() for t in form.get("add", "").split(",") if t.strip()]
        remove = [t.strip() for t in form.get("remove", "").split(",") if t.strip()]
        merge_tags(profile, add, remove)
    write_person_json(folder, profile)
    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    with session_scope(session_factory) as session:
        sync_person_profile_to_db(session, folder_name=slug, folder=folder)
    tag_dicts = tags_to_dicts(profile.tags)
    name_js = json.dumps(slug)
    tag_names = [
        str(t.get("tag") or "").strip()
        for t in tag_dicts
        if isinstance(t, dict) and str(t.get("tag") or "").strip()
    ]
    return {
        "ok": True,
        "message": _t("api.tags_updated", lang),
        "tags": tag_dicts,
        "tags_html": _tags_cell_html(name_js, tag_dicts, slug, lang=lang),
        "sort_tags": " ".join(tag_names).lower(),
        "tag_names": tag_names,
    }


def _list_people_rows() -> list[dict[str, object]]:
    """Overview = subfolders of the people root, joined with DB registration status."""
    root = _resolved_people_root()
    if root is None:
        with STATE.lock:
            STATE.people_has_mismatch = False
        return []

    if not STATE.people_root_last:
        STATE.set_last_paths(people_root=str(root))

    folder_names = _people_folder_names(root)
    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)

    # Read-only join — do not sync every person.json on each UI refresh (that was a
    # multi-second NAS/DB tax on every page that touched the People nav warn).
    by_name: dict[str, dict[str, object]] = {}
    with session_scope(session_factory) as session:
        stmt = (
            select(
                Person.name,
                Person.active,
                Person.display_name,
                Person.first_name,
                Person.last_name,
                Person.tags_json,
                Consent.consent_given,
                func.count(FaceEmbedding.id).label("emb_count"),
            )
            .outerjoin(Consent, Consent.person_id == Person.id)
            .outerjoin(FaceEmbedding, FaceEmbedding.person_id == Person.id)
            .group_by(
                Person.id,
                Person.name,
                Person.active,
                Person.display_name,
                Person.first_name,
                Person.last_name,
                Person.tags_json,
                Consent.consent_given,
            )
            .order_by(Person.name)
        )
        for row in session.execute(stmt).all():
            name, active, display_name, first_name, last_name, tags_json, consent_given, emb_count = row
            n = str(name)
            prev = by_name.get(n)
            if prev is not None and prev.get("active") and not active:
                continue
            by_name[n] = {
                "active": bool(active),
                "consent": bool(consent_given) if consent_given is not None else False,
                "embeddings": int(emb_count or 0),
                "display_name": str(display_name) if display_name else None,
                "first_name": str(first_name) if first_name else "",
                "last_name": str(last_name) if last_name else "",
                "tags": tags_to_dicts(tags_from_json(str(tags_json) if tags_json else None)),
            }

    out: list[dict[str, object]] = []
    # Parallelize NAS photo walks — sequential rglob dominates People page load time.
    photo_counts: dict[str, tuple[int, int]] = {}
    if folder_names:
        workers = min(8, len(folder_names))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_count_photos_in_person_folder, root / name): name
                for name in folder_names
            }
            for fut in concurrent.futures.as_completed(futs):
                name = futs[fut]
                try:
                    photo_counts[name] = fut.result()
                except Exception:
                    photo_counts[name] = (0, 0)
    for name in folder_names:
        total, gallery_n = photo_counts.get(name, (0, 0))
        info = by_name.get(name)
        profile = profile_for_folder(root / name, name)
        display = (
            (info.get("display_name") if info else None)
            or profile.display_name
            or display_name_from_slug(name)
        )
        tags = tags_to_dicts(profile.tags)
        if info is None:
            status = "No photos in folder" if total == 0 else "Not registered"
            consent = False
            embeddings = 0
            registered = False
        elif info["active"]:
            status = "Registered"
            consent = bool(info["consent"])
            embeddings = int(info["embeddings"])
            registered = True
        else:
            status = "Needs scan (embeddings kept)"
            consent = bool(info["consent"])
            embeddings = int(info["embeddings"])
            registered = False
        out.append(
            {
                "name": name,
                "display_name": str(display),
                "first_name": profile.first_name or (info.get("first_name") if info else ""),
                "last_name": profile.last_name or (info.get("last_name") if info else ""),
                "tags": list(tags) if isinstance(tags, list) else [],
                "photos": total,
                "gallery_photos": gallery_n,
                "embeddings": embeddings,
                "consent": consent,
                "status": status,
                "registered": registered,
                "active": bool(info["active"]) if info else False,
                "needs_reregister": _person_needs_reregister(total, embeddings),
            }
        )
    with STATE.lock:
        STATE.people_has_mismatch = any(bool(r.get("needs_reregister")) for r in out)
    return out


def _is_year_tag(name: str) -> bool:
    s = name.strip()
    return s.isdigit() and len(s) == 4


def _collapsed_visible_tag_names(names: list[str], *, limit: int = 8) -> set[str]:
    """Which tags survive collapsed truncation: custom tags first, then latest years."""
    if len(names) <= limit:
        return set(names)
    years = [n for n in names if _is_year_tag(n)]
    custom = [n for n in names if not _is_year_tag(n)]
    custom_sorted = sorted(custom, key=lambda s: s.casefold())
    if len(custom_sorted) >= limit:
        return set(custom_sorted[:limit])
    selected = list(custom_sorted)
    remaining = limit - len(selected)
    years_desc = sorted(years, key=lambda y: int(y), reverse=True)
    selected.extend(years_desc[:remaining])
    return set(selected)


def _display_ordered_tags(tags: list[dict[str, str]]) -> list[dict[str, str]]:
    """Years ascending, then custom tags alphabetically."""
    years: list[dict[str, str]] = []
    custom: list[dict[str, str]] = []
    for tag_row in tags:
        name = str(tag_row.get("tag") or "").strip()
        if not name:
            continue
        if _is_year_tag(name):
            years.append(tag_row)
        else:
            custom.append(tag_row)
    years.sort(key=lambda r: int(str(r.get("tag") or "0")))
    custom.sort(key=lambda r: str(r.get("tag") or "").casefold())
    return years + custom


def _people_name_display_parts(first_name: str, last_name: str, slug: str) -> tuple[str, str]:
    """Original-spelling first/last for display (slug fallback when profile fields empty)."""
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    if not first or not last:
        parts = slug.split("_", 1)
        if len(parts) == 2:
            if not last:
                last = parts[0].replace("-", " ")
            if not first:
                first = parts[1].replace("-", " ")
    return first, last


def _people_name_label(first: str, last: str, display_fallback: str, *, last_first: bool = False) -> str:
    if first and last:
        if last_first:
            return f"{last}, {first}"
        return f"{first} {last}"
    return display_fallback


def _people_name_sort_keys(first_name: str, last_name: str, slug: str) -> tuple[str, str]:
    """Lowercase folded keys for People-table name sorting."""
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    if not first or not last:
        parts = slug.split("_", 1)
        if len(parts) == 2:
            if not last:
                last = parts[0].replace("-", " ")
            if not first:
                first = parts[1].replace("-", " ")
    first_key = fold_german_umlauts(first).casefold()
    last_key = fold_german_umlauts(last).casefold()
    return first_key, last_key


def _people_name_column_header_html(lang: str = DEFAULT_LANG) -> str:
    """Combined name header (fixed ``Name · Surname``); parts toggle sort + row labels."""
    first_l = html.escape(_t("people.col.name_first", lang))
    last_l = html.escape(_t("people.col.name_last", lang))
    sep = html.escape(_t("people.col.name_sep", lang))
    return (
        f'<th class="sortable people-name-col sort-asc" '
        f'style="text-align:left" data-sort-key="name">'
        f'<span class="people-name-sort-label">'
        f'<button type="button" class="people-name-sort-part sort-active" '
        f'data-name-part="first" onclick="sortPeopleName(event, \'first\')">{first_l}</button>'
        f'<span class="people-name-sort-sep">{sep}</span>'
        f'<button type="button" class="people-name-sort-part" '
        f'data-name-part="last" onclick="sortPeopleName(event, \'last\')">{last_l}</button>'
        f"</span></th>"
    )


def _tag_pill_html(
    name_js: str, tag_name: str, consent: str, lang: str = DEFAULT_LANG
) -> str:
    consent_n = consent.strip().lower() if consent else "blocked"
    if consent_n not in ("blocked", "allowed", "none"):
        consent_n = "blocked"
    te = html.escape(tag_name)
    t_js = json.dumps(tag_name)
    tip = html.escape(_t("people.tag.consent_tip", lang, tag=tag_name))
    remove_aria = html.escape(_t("people.tag.remove_aria", lang))
    return (
        f'<span class="tag-pill tag-consent-{consent_n}" title="{tip}">'
        f"<span class='tag-body' onclick='cycleTagConsent(event, {name_js}, {t_js})'>{te}</span>"
        f"<button type='button' class='tag-remove' "
        f"onclick='removePersonTag({name_js}, {t_js})' aria-label='{remove_aria}'>&times;</button>"
        f"</span>"
    )


def _tag_rows_html(pills: list[str], add_btn: str | None = None) -> str:
    """Chunk pills into rows of 4; optional ``+`` inline on the last row only."""
    if not pills:
        if add_btn:
            return f'<div class="tag-row tag-row--last">{add_btn}</div>'
        return ""
    parts: list[str] = []
    for i in range(0, len(pills), 4):
        chunk = pills[i : i + 4]
        is_last = i + 4 >= len(pills)
        cls = "tag-row tag-row--last" if is_last else "tag-row"
        extra = add_btn if is_last and add_btn else ""
        parts.append(f'<div class="{cls}">{"".join(chunk)}{extra}</div>')
    return "".join(parts)


def _tags_cell_html(
    name_js: str, tags: list[dict[str, str]], slug: str = "", lang: str = DEFAULT_LANG
) -> str:
    ordered = _display_ordered_tags(tags)
    person_tag_names = [str(r.get("tag") or "").strip() for r in ordered]
    person_tag_names = [n for n in person_tag_names if n]
    total = len(person_tag_names)
    collapsed_visible = _collapsed_visible_tag_names(person_tag_names, limit=8)
    has_overflow = total > 8

    all_pills: list[str] = []
    collapsed_pills: list[str] = []
    for tag_row in ordered:
        tag_name = str(tag_row.get("tag") or "").strip()
        if not tag_name:
            continue
        consent = str(tag_row.get("consent") or "blocked")
        pill = _tag_pill_html(name_js, tag_name, consent, lang=lang)
        all_pills.append(pill)
        if tag_name in collapsed_visible:
            collapsed_pills.append(pill)

    names_js = json.dumps(person_tag_names)
    slug_attr = html.escape(slug, quote=True)
    add_btn = (
        f'<button type="button" class="tag-pill tag-add tag-add-cell" '
        f'data-person-slug="{slug_attr}" '
        f"onclick='openTagPicker(event, {name_js}, {names_js})'>+</button>"
    )
    # Tier C (>8 tags): ``+`` lives in the top row next to more/less (stable when toggling).
    # Tier A/B (<4 / 4–8): ``+`` inline after the last chip row.
    tier_overflow = has_overflow
    inline_add_btn: str | None = add_btn if not tier_overflow else None
    more_row = ""
    if tier_overflow:
        more_row = (
            f'<div class="tags-more-row">'
            f'<button type="button" class="tag-pill tag-more" '
            f"onclick='toggleTagsExpand(event, this)'>"
            f"{html.escape(_t('people.tag.more', lang))}</button>"
            f"{add_btn}"
            f"</div>"
        )
    overflow_cls = " has-overflow" if has_overflow else ""
    if tier_overflow:
        rows_html = (
            f'<div class="tag-rows tag-rows--collapsed">'
            f"{_tag_rows_html(collapsed_pills, None)}"
            f"</div>"
            f'<div class="tag-rows tag-rows--expanded">'
            f"{_tag_rows_html(all_pills, None)}"
            f"</div>"
        )
    else:
        rows_html = (
            f'<div class="tag-rows tag-rows--collapsed">'
            f"{_tag_rows_html(all_pills, inline_add_btn)}"
            f"</div>"
        )
    return (
        f'<div class="tags-cell{overflow_cls}" data-tags-cell="1" '
        f'data-total="{total}">'
        f"{more_row}"
        f"{rows_html}"
        f"</div>"
    )


def _people_table_body_html(
    rows: list[dict[str, object]] | None = None, lang: str = DEFAULT_LANG
) -> str:
    """HTML ``<tr>…</tr>`` rows for the People overview table."""
    colspan = "6"
    has_folder = bool(_resolved_people_root())
    if not has_folder:
        return (
            f"<tr><td colspan='{colspan}' style='color:var(--muted)'>"
            f"{html.escape(_t('people.empty.no_folder', lang))}"
            "</td></tr>"
        )
    if rows is None:
        rows = _list_people_rows()
    if not rows:
        return (
            f"<tr><td colspan='{colspan}' style='color:var(--muted)'>"
            f"{html.escape(_t('people.empty.no_subfolders', lang))}"
            "</td></tr>"
        )
    status_keys = {
        "Registered": "people.status.registered",
        "Not registered": "people.status.not_registered",
        "No photos in folder": "people.status.no_photos",
        "Needs scan (embeddings kept)": "people.status.needs_scan",
    }
    rows_html: list[str] = []
    for r in rows:
        name_raw = str(r["name"])
        display_raw = str(r.get("display_name") or name_raw)
        name_esc = html.escape(name_raw)
        display_esc = html.escape(display_raw)
        name_js = json.dumps(name_raw)
        profile_js = json.dumps(
            {
                "slug": name_raw,
                "first_name": str(r.get("first_name") or ""),
                "last_name": str(r.get("last_name") or ""),
                "display_name": display_raw,
                "consent": "allowed" if r.get("consent") else "blocked",
                "tags": r.get("tags") or [],
            }
        )
        registered = bool(r["registered"])
        photos = int(r["photos"])
        embeddings = int(r["embeddings"])
        needs_reregister = bool(r.get("needs_reregister"))
        status = str(r["status"])
        status_display = _t(status_keys[status], lang) if status in status_keys else status
        tags = [t for t in (r.get("tags") or []) if isinstance(t, dict)]
        tag_names = [str(t.get("tag") or "") for t in tags]
        consent_txt = _t("people.consent.allowed", lang) if r.get("consent") else _t("people.consent.blocked", lang)
        search_core = html.escape(
            " ".join(
                [
                    name_raw,
                    display_raw,
                    str(photos),
                    str(embeddings),
                    consent_txt,
                    status_display,
                ]
            ).lower()
        )
        search_tags = html.escape(" ".join(tag_names).lower())
        search_text = html.escape(
            " ".join(
                [
                    name_raw,
                    display_raw,
                    str(photos),
                    str(embeddings),
                    consent_txt,
                    " ".join(tag_names),
                    status_display,
                ]
            ).lower()
        )

        warn = ""
        if needs_reregister:
            tip = html.escape(_mismatch_tooltip(photos, embeddings, lang))
            warn = (
                f'<span class="people-mismatch-warn-after" title="{tip}" '
                f'aria-label="{html.escape(_t("people.mismatch.aria", lang))}">⚠️</span>'
            )
        status_sub = ""
        if status != "Registered":
            status_sub = f'<div class="people-name-sub">{html.escape(status_display)}</div>'
        slug_sub = ""
        if display_raw != name_raw:
            slug_sub = f'<div class="people-name-sub">{name_esc}</div>'

        first_disp, last_disp = _people_name_display_parts(
            str(r.get("first_name") or ""),
            str(r.get("last_name") or ""),
            name_raw,
        )
        name_label = _people_name_label(first_disp, last_disp, display_raw, last_first=False)
        name_label_esc = html.escape(name_label)

        name_cell = (
            f'<td class="people-name-cell">'
            f'<span class="people-name-line">'
            f"<a class='person-link' href='#' onclick='return openGallery({name_js})'>"
            f'<span class="person-link-label">{name_label_esc}</span></a>'
            f"{warn}"
            f"</span>{slug_sub}{status_sub}"
            f"</td>"
        )

        dash = html.escape(_t("people.dash", lang))
        if registered:
            consent_cls = "consent-allowed" if r["consent"] else "consent-blocked"
            set_allowed = "false" if r["consent"] else "true"
            flip_hint = (
                _t("people.consent.click_to_block", lang)
                if r["consent"]
                else _t("people.consent.click_to_allow", lang)
            )
            consent_cell = (
                f'<button type="button" class="consent-pill {consent_cls}" '
                f"onclick='peopleConsentToggle({name_js}, {set_allowed})' "
                f'title="{html.escape(flip_hint)}">'
                f"{html.escape(consent_txt)}</button>"
            )
            menu_items = (
                f"<button type='button' onclick='closePeopleMenus(); openEditPerson({profile_js});'>"
                f"{html.escape(_t('people.menu.edit', lang))}</button>"
                f"<button type='button' onclick='closePeopleMenus(); reregisterPerson({name_js});'>"
                f"{html.escape(_t('people.menu.reregister', lang))}</button>"
                f"<button type='button' class='menu-destructive' "
                f"onclick='closePeopleMenus(); peopleWipeByName({name_js});'>"
                f"{html.escape(_t('people.menu.wipe', lang))}</button>"
            )
        elif photos > 0:
            consent_cell = f'<span style="color:var(--muted)">{dash}</span>'
            menu_items = (
                f"<button type='button' onclick='closePeopleMenus(); openEditPerson({profile_js});'>"
                f"{html.escape(_t('people.menu.edit', lang))}</button>"
                f"<button type='button' onclick='closePeopleMenus(); reregisterPerson({name_js});'>"
                f"{html.escape(_t('people.menu.register', lang))}</button>"
            )
        else:
            consent_cell = f'<span style="color:var(--muted)">{dash}</span>'
            menu_items = (
                f"<button type='button' onclick='closePeopleMenus(); openEditPerson({profile_js});'>"
                f"{html.escape(_t('people.menu.edit', lang))}</button>"
            )

        actions_aria = html.escape(_t("people.menu.actions_aria", lang, display=display_raw))
        actions = (
            f'<div class="people-row-menu">'
            f'<button type="button" class="kebab-btn" aria-label="{actions_aria}" '
            f"onclick='togglePeopleRowMenu(event, this)'>⋮</button>"
            f'<div class="people-menu-panel" hidden>{menu_items}</div>'
            f"</div>"
        )

        tags_cell = _tags_cell_html(name_js, tags, name_raw, lang=lang)
        sort_first, sort_last = _people_name_sort_keys(
            str(r.get("first_name") or ""),
            str(r.get("last_name") or ""),
            name_raw,
        )
        sort_name = html.escape(display_raw.lower())
        sort_tags = html.escape(" ".join(tag_names).lower())
        sort_consent = "1" if r.get("consent") else "0"
        row_cls = ' class="embedding-mismatch"' if needs_reregister else ""
        rows_html.append(
            f"<tr{row_cls} data-person-name=\"{html.escape(name_raw.lower())}\" "
            f'data-search-text="{search_text}" '
            f'data-search-core="{search_core}" data-search-tags="{search_tags}" '
            f'data-sort-name="{sort_name}" '
            f'data-display-first="{html.escape(first_disp)}" '
            f'data-display-last="{html.escape(last_disp)}" '
            f'data-display-fallback="{html.escape(display_raw)}" '
            f'data-sort-first="{html.escape(sort_first)}" '
            f'data-sort-last="{html.escape(sort_last)}" '
            f'data-sort-photos="{photos}" '
            f'data-sort-faces="{embeddings}" data-sort-consent="{sort_consent}" '
            f'data-sort-tags="{sort_tags}">'
            f"{name_cell}"
            f"<td>{photos}</td>"
            f"<td>{embeddings}</td>"
            f"<td>{consent_cell}</td>"
            f"<td>{tags_cell}</td>"
            f'<td class="people-actions-cell">{actions}</td>'
            "</tr>"
        )
    return "".join(rows_html)


def _people_mismatch_warn_visible(rows: list[dict[str, object]] | None = None) -> bool:
    if rows is None:
        with STATE.lock:
            return bool(STATE.people_has_mismatch)
    return any(bool(r.get("needs_reregister")) for r in rows)


def _nav_people_link_html(
    *,
    active_cls: str,
    mismatch: bool | None = None,
    label: str = "People",
    mismatch_tip: str = "Some people have photos ≠ embeddings — use Re-register.",
) -> str:
    """People nav item; appends ⚠️ when any person needs Re-register (photos ≠ embeddings)."""
    if mismatch is None:
        mismatch = _people_mismatch_warn_visible()
    title = f' title="{html.escape(mismatch_tip)}"' if mismatch else ""
    warn_style = "" if mismatch else "display:none;"
    return (
        f'<a class="{active_cls}" id="nav_people_link" href="/people"{title}>'
        f'{html.escape(label)}<span id="nav_people_mismatch_warn" class="people-mismatch-warn-after" '
        f'style="{warn_style}">⚠️</span>'
        f"</a>"
    )


def _start_consent_update(name: str, *, consent_allowed: bool, lang: str = DEFAULT_LANG) -> dict[str, object]:
    name = name.strip()
    if not name:
        return {"ok": False, "error": _t("api.missing_person", lang)}
    if bool(STATE.snapshot()["running"]):
        return {"ok": False, "error": _t("api.job_running", lang)}

    set_to = "allowed" if consent_allowed else "blocked"

    def _worker() -> None:
        try:
            STATE.reset_for_run("Update consent + relabel")
            STATE.set_stage("Updating consent")
            STATE.add_activity(f"Toggling consent for {name}…")
            STATE.set_run_scope("person", name)

            settings = load_settings()
            _, session_factory = create_engine_and_session_factory(settings.database_url)
            metadata_sync = build_metadata_sync(
                settings,
                log=logging.getLogger("faceit_ai.metadata"),
                audit=logging.getLogger("faceit_ai.audit"),
            )

            res = run_redecide_and_sync_person(
                person_name=name,
                consent_allowed=consent_allowed,
                settings=settings,
                session_factory=session_factory,
                metadata=metadata_sync,
                audit=logging.getLogger("faceit_ai.audit"),
            )

            STATE.set_error_count(res.metadata_errors)
            STATE.set_summary("metadata_synced", res.metadata_applied)
            STATE.set_summary("metadata_no_db_match", 0)
            STATE.set_summary("metadata_skipped_status", 0)
            STATE.set_summary("metadata_errors", res.metadata_errors)
            STATE.add_activity(
                f"Done: consent set to {set_to}, metadata applied={res.metadata_applied}, "
                f"errors={res.metadata_errors}."
            )
            STATE.finish_run(True)
        except Exception as e:
            STATE.add_log(f"[error] consent sync failed: {e}")
            STATE.finish_run(False)

    threading.Thread(target=_worker, daemon=True).start()
    return {
        "ok": True,
        "syncing": True,
        "message": _t("api.consent_msg", lang, name=name),
    }


def _start_wipe_embeddings(name: str, *, lang: str = DEFAULT_LANG) -> dict[str, object]:
    name = name.strip()
    if not name:
        return {"ok": False, "error": _t("api.missing_person", lang)}
    if bool(STATE.snapshot()["running"]):
        return {"ok": False, "error": _t("api.job_running", lang)}

    def _worker() -> None:
        try:
            STATE.reset_for_run("Wipe embeddings")
            STATE.set_stage("Wiping embeddings")
            STATE.add_activity(f"Wiping embeddings for {name}…")

            settings = load_settings()
            _, session_factory = create_engine_and_session_factory(settings.database_url)
            with session_scope(session_factory) as session:
                person_id = session.scalar(select(Person.id).where(Person.name == name))
                if person_id is None:
                    raise ValueError(f"No person named {name!r}")
                session.execute(
                    FaceEmbedding.__table__.delete().where(
                        FaceEmbedding.person_id == int(person_id)
                    )
                )
                session.execute(
                    Person.__table__.update()
                    .where(Person.id == int(person_id))
                    .values(active=False)
                )

            STATE.set_stage("Updating consent + relabel")
            metadata_sync = build_metadata_sync(
                settings,
                log=logging.getLogger("faceit_ai.metadata"),
                audit=logging.getLogger("faceit_ai.audit"),
            )
            res = run_redecide_and_sync_person(
                person_name=name,
                consent_allowed=False,
                settings=settings,
                session_factory=session_factory,
                metadata=metadata_sync,
                audit=logging.getLogger("faceit_ai.audit"),
            )
            STATE.set_error_count(res.metadata_errors)
            STATE.set_summary("metadata_synced", res.metadata_applied)
            STATE.set_summary("metadata_no_db_match", 0)
            STATE.set_summary("metadata_skipped_status", 0)
            STATE.set_summary("metadata_errors", res.metadata_errors)
            STATE.add_activity(
                f"Done: wiped embeddings for {name}, metadata applied={res.metadata_applied}."
            )
            STATE.finish_run(True)
        except Exception as e:
            STATE.add_log(f"[error] delete failed: {e}")
            STATE.finish_run(False)

    threading.Thread(target=_worker, daemon=True).start()
    return {
        "ok": True,
        "syncing": True,
        "message": _t("api.wipe_msg", lang, name=name),
    }


def _safe_person_dir(name: str) -> Path | None:
    """Resolve people_root/name with path-traversal protection."""
    root = _resolved_people_root()
    if root is None:
        return None
    name = name.strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    person_dir = (root / name).resolve()
    try:
        person_dir.relative_to(root)
    except ValueError:
        return None
    return person_dir if person_dir.is_dir() else None


def _list_person_gallery_paths(name: str) -> list[Path]:
    person_dir = _safe_person_dir(name)
    if person_dir is None:
        return []
    try:
        settings = load_settings()
        paths = list_scannable_image_paths(
            person_dir,
            extensions=settings.pipeline.image.scan_extensions(),
            ignore_filename_substrings=settings.pipeline.image.ignore_filename_substrings,
        )
    except Exception:
        paths = [p for p in person_dir.rglob("*") if p.is_file()]
    return [p for p in paths if p.suffix.lower() in _GALLERY_EXTENSIONS]


def _safe_gallery_file(path_text: str) -> Path | None:
    """Serve only files under the configured people root."""
    root = _resolved_people_root()
    if root is None:
        return None
    try:
        path = Path(unquote(path_text)).expanduser().resolve()
    except Exception:
        return None
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file() or path.suffix.lower() not in _GALLERY_EXTENSIONS:
        return None
    return path


def _delete_person_gallery_file(name: str, path_text: str, *, lang: str = DEFAULT_LANG) -> dict[str, object]:
    """Remove one browser-viewable photo from ``people_root/<name>/`` on disk."""
    person_dir = _safe_person_dir(name)
    if person_dir is None:
        return {"ok": False, "error": f"Invalid or missing person folder for {name!r}."}
    file_path = _safe_gallery_file(path_text)
    if file_path is None:
        return {"ok": False, "error": "Invalid or missing file path."}
    try:
        file_path.relative_to(person_dir)
    except ValueError:
        return {"ok": False, "error": "File is not in this person's folder."}
    if not file_path.is_file():
        return {"ok": False, "error": "File not found on disk."}
    try:
        file_path.unlink()
    except OSError as e:
        return {"ok": False, "error": f"Could not delete file: {e}"}
    try:
        from faceit_ai.services.collected_photos import delete_collected_photo

        settings = load_settings()
        _, session_factory = create_engine_and_session_factory(settings.database_url)
        with session_scope(session_factory) as session:
            delete_collected_photo(session, file_path)
    except Exception as e:
        STATE.add_log(f"[warn] could not remove collected_photo link: {e}")
    return {"ok": True, "message": _t("api.gallery_deleted", lang, file=file_path.name, name=name)}


def _safe_review_folder(folder_text: str) -> Path | None:
    text = (folder_text or "").strip()
    if not text:
        return None
    p = Path(text).expanduser().resolve()
    return p if p.is_dir() else None


def _parse_review_status(raw: str) -> str:
    s = (raw or "review").strip().lower()
    return s if s in ("review", "blocked") else "review"


def _list_people_names_for_review() -> list[str]:
    root = _resolved_people_root()
    if root is None:
        return []
    return _people_folder_names(root)


def _review_people_payload() -> dict[str, object]:
    entries = _list_people_for_review()
    labels = {e["slug"]: e["display_name"] for e in entries}
    return {
        "people_names": [e["slug"] for e in entries],
        "people_entries": entries,
        "people_labels": labels,
    }


def _review_photos_response(
    folder_text: str, status: str = "review", *, lang: str = DEFAULT_LANG
) -> dict[str, object]:
    folder = _safe_review_folder(folder_text)
    if folder is None:
        return {"ok": False, "error": _t("api.invalid_folder", lang)}
    want = _parse_review_status(status)
    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    with session_scope(session_factory) as session:
        photos = list_review_assets_json(session, folder, status=want)  # type: ignore[arg-type]
        status_counts = count_review_assets_by_status(session, folder)
    folder_s = str(folder)
    for photo in photos:
        photo["preview_url"] = (
            f"/api/review_photo/preview?id={photo['asset_id']}"
            f"&folder={quote(folder_s)}&status={want}"
        )
    return {
        "ok": True,
        "folder": folder_s,
        "status": want,
        "count": len(photos),
        "status_counts": status_counts,
        "photos": photos,
        **_review_people_payload(),
    }


def _review_photo_detail(
    asset_id: int, folder_text: str, status: str = "review", *, lang: str = DEFAULT_LANG
) -> dict[str, object]:
    folder = _safe_review_folder(folder_text)
    if folder is None:
        return {"ok": False, "error": _t("api.invalid_folder", lang)}
    if asset_id <= 0:
        return {"ok": False, "error": _t("api.invalid_asset", lang)}
    want = _parse_review_status(status)
    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    with session_scope(session_factory) as session:
        detail = load_review_asset_detail(
            session,
            asset_id,
            folder,
            image_cfg=settings.pipeline.image,
            status=want,  # type: ignore[arg-type]
        )
    if detail is None:
        return {"ok": False, "error": _t("api.photo_not_found", lang)}
    folder_slugs = {e["slug"] for e in _list_people_for_review()}
    return {
        "ok": True,
        "asset_id": detail.asset_id,
        "path": detail.path,
        "name": Path(detail.path).name,
        "reason": detail.reason,
        "status": want,
        "missing_on_disk": detail.missing_on_disk,
        "preview_w": detail.preview_w,
        "preview_h": detail.preview_h,
        "preview_url": (
            f"/api/review_photo/preview?id={detail.asset_id}"
            f"&folder={quote(str(folder))}&status={want}"
        ),
        "faces": [
            {
                "face_id": f.face_id,
                "bbox": f.bbox,
                "person_name": f.person_name,
                "match_score": f.match_score,
                "in_people_folder": bool(
                    f.person_name and str(f.person_name).strip() in folder_slugs
                ),
            }
            for f in detail.faces
        ],
        **_review_people_payload(),
    }


def _parse_face_assignments(raw: str) -> list[FaceAssignment] | None:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    out: list[FaceAssignment] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        face_id = item.get("face_id")
        person_name = str(item.get("person_name", "")).strip()
        if not isinstance(face_id, int) and not (
            isinstance(face_id, str) and str(face_id).isdigit()
        ):
            return None
        if not person_name:
            return None
        out.append(FaceAssignment(face_id=int(face_id), person_name=person_name))
    return out


def _parse_face_assignment_updates(raw: str) -> list[FaceAssignment] | None:
    """Like ``_parse_face_assignments`` but allows empty person_name (clear/Unknown)."""
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    out: list[FaceAssignment] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        face_id = item.get("face_id")
        person_name = str(item.get("person_name", "")).strip()
        if not isinstance(face_id, int) and not (
            isinstance(face_id, str) and str(face_id).isdigit()
        ):
            return None
        out.append(FaceAssignment(face_id=int(face_id), person_name=person_name))
    return out


def _save_review_face_assignments_request(
    asset_id: int,
    folder_text: str,
    faces_json: str,
    status: str = "review",
    *,
    lang: str = DEFAULT_LANG,
) -> dict[str, object]:
    folder = _safe_review_folder(folder_text)
    if folder is None:
        return {"ok": False, "error": _t("api.invalid_folder", lang)}
    if asset_id <= 0:
        return {"ok": False, "error": _t("api.invalid_asset", lang)}
    assignments = _parse_face_assignment_updates(faces_json)
    if not assignments:
        return {"ok": False, "error": _t("api.invalid_faces", lang)}

    for a in assignments:
        n = a.person_name.strip()
        if n and (n in (".", "..") or "/" in n or "\\" in n):
            return {"ok": False, "error": _t("api.invalid_person_name", lang, name=a.person_name)}

    want = _parse_review_status(status)
    settings = load_settings()
    people_root = _resolved_people_root()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    audit = logging.getLogger("faceit_ai.audit")
    export_mode = settings.export.flagged
    if export_mode not in ("copy", "move"):
        export_mode = "off"
    metadata = build_metadata_sync(
        settings,
        log=logging.getLogger("faceit_ai.metadata"),
        audit=audit,
    )
    backend = _get_insightface_backend(settings)
    try:
        with session_scope(session_factory) as session:
            result = save_review_face_assignments(
                session=session,
                asset_id=asset_id,
                folder=folder,
                face_assignments=assignments,
                image_cfg=settings.pipeline.image,
                status=want,  # type: ignore[arg-type]
                settings=settings,
                people_root=people_root,
                audit=audit,
                backend=backend,
                session_factory=session_factory,
                metadata=metadata,
                export_flagged=export_mode,  # type: ignore[arg-type]
            )
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logging.getLogger("faceit_ai").exception("save face assignments failed")
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "updated": result.updated,
        "crops_written": result.crops_written,
        "embeddings_added": result.embeddings_added,
        "reprocessed": result.reprocessed,
        "new_status": result.new_status,
        "new_reason": result.new_reason,
        "flagged_pruned": result.flagged_pruned,
    }


def _confirm_review_blocked_request(
    asset_id: int,
    folder_text: str,
    faces_json: str,
    *,
    status: str = "review",
    lang: str = DEFAULT_LANG,
) -> dict[str, object]:
    folder = _safe_review_folder(folder_text)
    if folder is None:
        return {"ok": False, "error": _t("api.invalid_folder", lang)}
    if asset_id <= 0:
        return {"ok": False, "error": _t("api.invalid_asset", lang)}
    assignments = _parse_face_assignments(faces_json)
    if not assignments:
        return {"ok": False, "error": _t("api.invalid_faces", lang)}

    for a in assignments:
        n = a.person_name.strip()
        if not n or n in (".", "..") or "/" in n or "\\" in n:
            return {"ok": False, "error": _t("api.invalid_person_name", lang, name=a.person_name)}

    people_root = _resolved_people_root()
    if people_root is None:
        return {"ok": False, "error": _t("api.people_not_configured", lang)}

    want = _parse_review_status(status)
    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    export_mode = settings.export.flagged
    if export_mode not in ("copy", "move"):
        export_mode = "off"

    audit = logging.getLogger("faceit_ai.audit")
    metadata = build_metadata_sync(
        settings,
        log=logging.getLogger("faceit_ai.metadata"),
        audit=audit,
    )
    backend = _get_insightface_backend(settings)
    try:
        with session_scope(session_factory) as session:
            result = confirm_review_blocked(
                session=session,
                asset_id=asset_id,
                folder=folder,
                face_assignments=assignments,
                settings=settings,
                people_root=people_root,
                metadata=metadata,
                export_action=export_mode,  # type: ignore[arg-type]
                audit=audit,
                status=want,  # type: ignore[arg-type]
                backend=backend,
            )
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logging.getLogger("faceit_ai").exception("review confirm failed")
        return {"ok": False, "error": str(e)}

    msg_key = "api.add_faces_ok" if want == "blocked" else "api.blocked_ok"
    return {
        "ok": True,
        "message": _t(
            msg_key,
            lang,
            c=result.crops_written,
            e=result.embeddings_added,
        ),
        "crops_written": result.crops_written,
        "embeddings_added": result.embeddings_added,
        "exported": result.exported,
        "metadata_applied": result.metadata_applied,
    }


def _batch_confirm_review_blocked_request(folder_text: str, *, lang: str = DEFAULT_LANG) -> dict[str, object]:
    folder = _safe_review_folder(folder_text)
    if folder is None:
        return {"ok": False, "error": _t("api.invalid_folder", lang)}

    people_root = _resolved_people_root()
    if people_root is None:
        return {"ok": False, "error": _t("api.people_not_configured", lang)}

    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    export_mode = settings.export.flagged
    if export_mode not in ("copy", "move"):
        export_mode = "off"

    audit = logging.getLogger("faceit_ai.audit")
    metadata = build_metadata_sync(
        settings,
        log=logging.getLogger("faceit_ai.metadata"),
        audit=audit,
    )
    try:
        result = batch_confirm_review_blocked(
            session_factory=session_factory,
            folder=folder,
            settings=settings,
            people_root=people_root,
            metadata=metadata,
            export_action=export_mode,  # type: ignore[arg-type]
            audit=audit,
        )
    except Exception as e:
        logging.getLogger("faceit_ai").exception("batch review confirm failed")
        return {"ok": False, "error": str(e)}

    parts: list[str] = []
    if result.moved:
        parts.append(f"{result.moved} moved to blocked")
    if result.skipped:
        parts.append(f"{result.skipped} skipped")
    if result.errors:
        parts.append(f"{result.errors} failed")
    message = "; ".join(parts) if parts else _t("api.batch_none", lang)
    if result.moved:
        message += (
            f" ({result.total_crops} crop(s), {result.total_embeddings} embedding(s) added)"
        )

    return {
        "ok": True,
        "message": message,
        "moved": result.moved,
        "skipped": result.skipped,
        "errors": result.errors,
        "total_crops": result.total_crops,
        "total_embeddings": result.total_embeddings,
        "skipped_items": list(result.skipped_items),
        "error_items": list(result.error_items),
    }


def _confirm_blocked_ok_request(asset_id: int, folder_text: str, *, lang: str = DEFAULT_LANG) -> dict[str, object]:
    folder = _safe_review_folder(folder_text)
    if folder is None:
        return {"ok": False, "error": _t("api.invalid_folder", lang)}
    if asset_id <= 0:
        return {"ok": False, "error": _t("api.invalid_asset", lang)}

    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    audit = logging.getLogger("faceit_ai.audit")
    metadata = build_metadata_sync(
        settings,
        log=logging.getLogger("faceit_ai.metadata"),
        audit=audit,
    )
    try:
        with session_scope(session_factory) as session:
            result = confirm_blocked_ok(
                session=session,
                asset_id=asset_id,
                folder=folder,
                settings=settings,
                metadata=metadata,
                audit=audit,
            )
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logging.getLogger("faceit_ai").exception("confirm blocked ok failed")
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "message": _t("api.moved_ok", lang),
        "metadata_applied": result.metadata_applied,
    }


def _confirm_review_ok_request(asset_id: int, folder_text: str, *, lang: str = DEFAULT_LANG) -> dict[str, object]:
    folder = _safe_review_folder(folder_text)
    if folder is None:
        return {"ok": False, "error": _t("api.invalid_folder", lang)}
    if asset_id <= 0:
        return {"ok": False, "error": _t("api.invalid_asset", lang)}

    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    audit = logging.getLogger("faceit_ai.audit")
    metadata = build_metadata_sync(
        settings,
        log=logging.getLogger("faceit_ai.metadata"),
        audit=audit,
    )
    try:
        with session_scope(session_factory) as session:
            result = confirm_review_ok(
                session=session,
                asset_id=asset_id,
                folder=folder,
                settings=settings,
                metadata=metadata,
                audit=audit,
            )
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logging.getLogger("faceit_ai").exception("confirm review ok failed")
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "message": _t("api.moved_ok_unknown", lang),
        "metadata_applied": result.metadata_applied,
    }


def _set_person_consent(person_name: str, allowed: bool) -> str:
    settings = load_settings()
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    with session_scope(session_factory) as session:
        repo = ConsentRepository(session)
        repo.update_consent_for_person_name(name=person_name, consent_given=allowed)
    return f"Updated consent for {person_name}: {'Allowed' if allowed else 'Blocked'}"


def _mask_db_url(url: str) -> str:
    """Hide any password in a SQLAlchemy URL for display."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            creds, host = rest.split("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
    return url


def _current_db_label() -> str:
    """Friendly description of the effective database backend."""
    try:
        url = load_settings().database_url
    except Exception:
        return "unknown"
    if url.startswith("sqlite:"):
        return f"SQLite (local file): {url.replace('sqlite:///', '')}"
    return f"Server: {_mask_db_url(url)}"


def _db_identity() -> dict[str, object]:
    """Short identity so operators can verify two PCs hit the same database."""
    try:
        settings = load_settings()
        url = settings.database_url
    except Exception as e:
        return {"ok": False, "error": str(e), "backend": "unknown", "shared": False}
    if url.startswith("sqlite:"):
        return {
            "ok": True,
            "backend": "sqlite",
            "shared": False,
            "label": _current_db_label(),
            "detail": url.replace("sqlite:///", ""),
        }
    detail = _mask_db_url(url)
    try:
        _, session_factory = create_engine_and_session_factory(url)
        with session_scope(session_factory) as session:
            # Postgres: prove which server/db we're on (visible across PCs).
            try:
                row = session.execute(
                    text(
                        "SELECT current_database() AS db, "
                        "COALESCE(inet_server_addr()::text, '') AS addr, "
                        "COALESCE(inet_server_port()::text, '') AS port"
                    )
                ).mappings().first()
                if row is not None:
                    db = str(row.get("db") or "")
                    addr = str(row.get("addr") or "")
                    port = str(row.get("port") or "")
                    where = f"{db} @ {addr}:{port}" if addr else db
                    detail = where or detail
            except Exception:
                pass
    except Exception as e:
        return {
            "ok": False,
            "backend": "server",
            "shared": True,
            "label": _current_db_label(),
            "detail": detail,
            "error": str(e),
        }
    return {
        "ok": True,
        "backend": "server",
        "shared": True,
        "label": _current_db_label(),
        "detail": detail,
    }


_ACTIVE_RUNS_CACHE: dict[str, object] = {"ts": 0.0, "runs": [], "url": ""}
_ACTIVE_RUNS_LOCK = threading.Lock()


def _get_active_runs_cached() -> list[dict[str, object]]:
    """Active analysis runs across all machines, queried at most every few seconds."""
    now = time.time()
    try:
        url = load_settings().database_url
    except Exception:
        url = ""
    with _ACTIVE_RUNS_LOCK:
        if (
            url
            and url == str(_ACTIVE_RUNS_CACHE.get("url") or "")
            and now - float(_ACTIVE_RUNS_CACHE["ts"]) < 3.0
        ):
            return list(_ACTIVE_RUNS_CACHE["runs"])  # type: ignore[arg-type]
    try:
        settings = load_settings()
        _, session_factory = create_engine_and_session_factory(settings.database_url)
        runs = list_active_runs(session_factory)
    except Exception as e:
        logging.getLogger("faceit_ai").warning("active runs query failed: %s", e)
        runs = []
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS_CACHE["ts"] = now
        _ACTIVE_RUNS_CACHE["runs"] = runs
        _ACTIVE_RUNS_CACHE["url"] = url
    return runs


def _test_db_connection(url: str, *, lang: str = DEFAULT_LANG) -> dict[str, object]:
    """Try to connect to the given DB URL (or the effective one if empty)."""
    url = os.path.expandvars((url or "").strip())
    if not url:
        try:
            url = load_settings().database_url
        except Exception as e:
            return {"ok": False, "error": f"could not resolve configured database: {e}"}
    backend = url.split("://", 1)[0] if "://" in url else "database"
    looks_postgres = "postgresql" in backend.lower() or "postgres" in backend.lower()
    try:
        engine = create_engine(url, future=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return {"ok": True, "backend": backend}
    except ModuleNotFoundError as e:
        missing = str(getattr(e, "name", "") or e)
        if looks_postgres or "psycopg" in missing.lower():
            return {"ok": False, "error": _t("settings.db.missing_psycopg", lang)}
        msg = str(e).splitlines()[0] if str(e) else e.__class__.__name__
        return {"ok": False, "error": f"{e.__class__.__name__}: {msg}"}
    except Exception as e:  # surface the driver error to the operator
        msg = str(e).splitlines()[0] if str(e) else e.__class__.__name__
        if looks_postgres and "psycopg" in msg.lower():
            return {"ok": False, "error": _t("settings.db.missing_psycopg", lang)}
        return {"ok": False, "error": f"{e.__class__.__name__}: {msg}"}


def _read_config_form() -> dict[str, object]:
    cfg = yaml.safe_load(STATE.config_path.read_text(encoding="utf-8")) or {}
    pi = (cfg.get("pipeline") or {}).get("insightface") or {}
    img = (cfg.get("pipeline") or {}).get("image") or {}
    md = cfg.get("metadata") or {}
    lg = cfg.get("logging") or {}
    lr = cfg.get("lightroom") or {}
    db = cfg.get("database") or {}
    paths = cfg.get("paths") or {}
    an = cfg.get("analyze") or {}
    ex = cfg.get("export") or {}
    col = cfg.get("collect") or {}
    det = pi.get("det_size", [512, 512])
    det_text = "512,512"
    if isinstance(det, (list, tuple)) and len(det) == 2:
        det_text = f"{int(det[0])},{int(det[1])}"
    providers_raw = pi.get("providers") or ["auto"]
    if isinstance(providers_raw, (list, tuple)):
        inference_providers = ", ".join(str(p) for p in providers_raw)
    else:
        inference_providers = str(providers_raw)
    flagged_raw = str(ex.get("flagged", "copy")).lower()
    if flagged_raw in ("false", "0", "no", ""):
        flagged_raw = "off"
    if flagged_raw not in ("off", "copy", "move"):
        flagged_raw = "copy"
    statuses_raw = ex.get("flagged_status")
    if statuses_raw is None:
        status_set = {"blocked", "review"}
    elif isinstance(statuses_raw, (list, tuple)):
        status_set = {str(x).lower() for x in statuses_raw}
    else:
        status_set = set()
    meta_sync = bool(an.get("sync_metadata_default", md.get("enabled", False)))
    ing = cfg.get("ingest") or {}
    return {
        "det_size": det_text,
        "inference_providers": inference_providers,
        "max_dimension": str(int(img.get("max_dimension", 1800))),
        "raw_decode_size": parse_raw_decode_size(img),
        "metadata_enabled": meta_sync,
        "verify_after_write": bool(md.get("exiftool_verify_after_write", False)),
        "exiftool_path": str(md.get("exiftool_path", "exiftool")),
        "debug_logging": str(lg.get("level", "INFO")).upper() == "DEBUG",
        "yaml_preview": yaml.safe_dump(cfg, sort_keys=False, allow_unicode=False),
        "label_blocked": str((lr.get("xmp_label_values") or {}).get("blocked", "Rot") or "None"),
        "label_review": str((lr.get("xmp_label_values") or {}).get("review", "Lila") or "None"),
        "label_ok": str((lr.get("xmp_label_values") or {}).get("ok", "") or "None"),
        "data_dir": str(paths.get("data_dir", "") or ""),
        "database_url": str(db.get("url", "") or ""),
        "current_db": _current_db_label(),
        "force_default": bool(an.get("force_default", False)),
        "sync_metadata_default": meta_sync,
        "export_flagged": flagged_raw,
        "export_status_blocked": "blocked" in status_set,
        "export_status_review": "review" in status_set,
        "collect_crop_portrait": bool(col.get("crop_portrait", True)),
        "ingest_enabled": bool(ing.get("enabled", False)),
        "ingest_order": (
            "analyze_then_copy"
            if str(ing.get("order") or "").strip().lower()
            in ("analyze_then_copy", "analyze-then-copy")
            else "copy_then_analyze"
        ),
    }


def _save_config(form: dict[str, str]) -> str:
    raw = yaml.safe_load(STATE.config_path.read_text(encoding="utf-8")) or {}
    parts = [p.strip() for p in form.get("det_size", "").split(",")]
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return "Invalid det_size (use 512,512)"
    max_dim = form.get("max_dimension", "").strip()
    if not max_dim.isdigit():
        return "Invalid max_dimension"
    raw.setdefault("pipeline", {}).setdefault("insightface", {})["det_size"] = [
        int(parts[0]),
        int(parts[1]),
    ]
    prov_text = form.get("inference_providers", "").strip() or "auto"
    providers = [p.strip() for p in prov_text.split(",") if p.strip()]
    raw.setdefault("pipeline", {}).setdefault("insightface", {})["providers"] = providers or ["auto"]
    raw.setdefault("pipeline", {}).setdefault("image", {})["max_dimension"] = int(max_dim)
    img_cfg = raw.setdefault("pipeline", {}).setdefault("image", {})
    decode_raw = (form.get("raw_decode_size") or "half").strip().lower()
    if decode_raw not in ("full", "half", "quarter"):
        decode_raw = "half"
    img_cfg["raw_decode_size"] = decode_raw
    img_cfg.pop("raw_half_size", None)
    meta_on = form.get("sync_metadata_default") == "on"
    raw.setdefault("metadata", {})["enabled"] = meta_on
    raw.setdefault("metadata", {})["exiftool_verify_after_write"] = (
        form.get("verify_after_write") == "on"
    )
    raw.setdefault("metadata", {})["exiftool_path"] = (
        form.get("exiftool_path", "").strip() or "exiftool"
    )
    raw.setdefault("logging", {})["level"] = "DEBUG" if form.get("debug_logging") == "on" else "INFO"
    # Shared data folder + database connection (multi-PC).
    raw.setdefault("paths", {})["data_dir"] = form.get("data_dir", "").strip()
    raw.setdefault("database", {})["url"] = form.get("database_url", "").strip()
    # Analyze page defaults (web UI).
    raw.setdefault("analyze", {})
    raw["analyze"]["force_default"] = form.get("force_default") == "on"
    raw["analyze"]["sync_metadata_default"] = meta_on
    export_mode = (form.get("export_flagged") or "off").strip().lower()
    if export_mode not in ("off", "copy", "move"):
        export_mode = "off"
    raw.setdefault("export", {})["flagged"] = export_mode
    flagged_statuses: list[str] = []
    if form.get("export_status_blocked") == "on":
        flagged_statuses.append("blocked")
    if form.get("export_status_review") == "on":
        flagged_statuses.append("review")
    raw["export"]["flagged_status"] = flagged_statuses
    raw.setdefault("ingest", {})
    raw["ingest"]["enabled"] = form.get("ingest_enabled") == "on"
    order_raw = (form.get("ingest_order") or "copy_then_analyze").strip()
    raw["ingest"]["order"] = (
        "analyze_then_copy" if order_raw == "analyze_then_copy" else "copy_then_analyze"
    )
    raw.setdefault("collect", {})["crop_portrait"] = form.get("collect_crop_portrait") == "on"
    labels = {
        "blocked": form.get("label_blocked", "Rot").strip() or "None",
        "review": form.get("label_review", "Lila").strip() or "None",
        "ok": form.get("label_ok", "None").strip() or "None",
    }
    raw.setdefault("lightroom", {}).setdefault("xmp_label_values", {})
    raw["lightroom"]["xmp_label_values"] = {
        "blocked": "" if labels["blocked"] == "None" else labels["blocked"],
        "review": "" if labels["review"] == "None" else labels["review"],
        "ok": "" if labels["ok"] == "None" else labels["ok"],
    }
    # Keep Lightroom XMP labels in German from the UI.
    # Do not translate to Photoshop LabelColor tokens (operator requested "no mapping").
    raw.setdefault("metadata", {}).setdefault("color_labels", {})
    for st, lab in labels.items():
        if lab == "None":
            raw["metadata"]["color_labels"][st] = {"xmp_label": None, "photoshop_label_color": None}
        else:
            raw["metadata"]["color_labels"][st] = {
                "xmp_label": lab,
                "photoshop_label_color": None,
            }
    STATE.config_path.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=False), encoding="utf-8"
    )
    return f"Saved settings to {STATE.config_path}"


class Handler(BaseHTTPRequestHandler):
    def _base_style(self) -> str:
        return """
<link rel="icon" href="/favicon.png" type="image/png" sizes="32x32">
<style>
:root {
  --bg: #111318;
  --panel: #181c24;
  --line: #2e3545;
  --text: #e8edf7;
  --muted: #9ea9bc;
  --accent: #5e96ff;
  --good: #3cc087;
  --warn: #f2a93b;
  --bad: #ef6b73;
  --label-col-width: 200px;
  --control-height: 40px;
  --dropdown-width: 160px;
  --row-gap: 20px;
  --section-gap: 24px;
  --section-padding: 24px;
  --checkbox-col-width: 280px;
  --checkbox-label-gap: 8px;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 20px;
  font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--text);
}
.wrap { max-width: 1120px; margin: 0 auto; }
h1 {
  margin: 0 0 6px;
  font-size: 26px;
  font-weight: 700;
  letter-spacing: 0.2px;
}
.subtitle { color: var(--muted); margin-bottom: 10px; }
.menu { display:flex; gap: 8px; margin: 8px 0 16px; align-items: center; }
.menu a {
  color: var(--text);
  text-decoration: none;
  border: 1px solid var(--line);
  background: #141924;
  padding: 0 14px;
  height: 36px;
  display: inline-flex;
  align-items: center;
  border-radius: 9px;
  font-weight: 600;
}
.menu a.active { background: var(--accent); border-color: transparent; color: #fff; }
.people-mismatch-warn-after { margin-left: 0.85em; }
.people-list-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.people-list-title { font-weight: 600; font-size: 15px; }
#people_search {
  margin-left: auto;
  min-width: 220px;
  max-width: 320px;
  flex: 1 1 220px;
  height: 36px;
  padding: 0 12px;
  border-radius: 9px;
  border: 1px solid var(--line);
  background: #141924;
  color: var(--text);
}
.people-name-line { display: inline-flex; align-items: center; vertical-align: middle; }
.people-name-sub { font-size: 12px; color: var(--muted); margin-top: 3px; }
.consent-pill {
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  background: #141924;
  color: var(--text);
}
.consent-pill.consent-allowed { color: #3cc087; border-color: #2a6b4f; }
.consent-pill.consent-blocked { color: #ef6b73; border-color: #7b2432; }
.consent-pill:hover { filter: brightness(1.08); }
.people-actions-cell { width: 48px; text-align: right; }
.people-row-menu { position: relative; display: inline-block; }
.kebab-btn {
  width: 32px;
  height: 32px;
  padding: 0;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: #141924;
  color: var(--text);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
}
.kebab-btn:hover { background: #1a2030; }
.people-menu-panel {
  position: absolute;
  right: 0;
  top: calc(100% + 4px);
  z-index: 30;
  background: #141924;
  border: 1px solid var(--line);
  border-radius: 8px;
  min-width: 168px;
  padding: 4px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.35);
}
.people-menu-panel button {
  display: block;
  width: 100%;
  text-align: left;
  margin: 0;
  padding: 8px 10px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text);
  cursor: pointer;
  font-size: 13px;
}
.people-menu-panel button:hover { background: #1a2030; }
.people-menu-panel .menu-destructive { color: #ef6b73; }
.col-help {
  position: relative;
  color: var(--muted);
  cursor: help;
  font-size: 11px;
  font-weight: normal;
  margin-left: 2px;
  display: inline;
}
.col-help::after {
  content: attr(data-tip);
  position: absolute;
  z-index: 999;
  left: 0;
  top: calc(100% + 8px);
  min-width: 240px;
  max-width: 380px;
  white-space: pre-line;
  background: #0b1220;
  color: #edf3ff;
  border: 1px solid #3d4e70;
  border-radius: 10px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.4;
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.35);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.18s ease;
  transition-delay: 0s;
}
.col-help:hover::after {
  opacity: 1;
  transition-delay: 0.35s;
}
.people-table th, .people-table td { padding: 10px 8px; vertical-align: middle; }
.people-table tbody tr { border-top: 1px solid var(--line); }
.people-table tbody tr.embedding-mismatch { background: rgba(239, 107, 115, 0.04); }
.people-table th.sortable { cursor: pointer; user-select: none; }
.people-table th.sortable:hover { color: var(--accent); }
.people-table th.sort-asc::after { content: " ▲"; font-size: 10px; }
.people-table th.sort-desc::after { content: " ▼"; font-size: 10px; }
.people-table th.people-name-col.sort-asc::after,
.people-table th.people-name-col.sort-desc::after { content: none; }
.people-name-col .people-name-sort-label {
  display: inline-flex;
  align-items: center;
  gap: 0.35em;
}
.people-name-sort-sep {
  color: var(--muted);
  user-select: none;
}
.people-name-sort-part {
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  color: inherit;
  cursor: pointer;
}
.people-name-sort-part:hover { color: var(--accent); }
.people-name-sort-part.sort-active { color: var(--accent); }
.people-name-col.sort-asc .people-name-sort-part.sort-active::after {
  content: " ▲";
  font-size: 10px;
}
.people-name-col.sort-desc .people-name-sort-part.sort-active::after {
  content: " ▼";
  font-size: 10px;
}
.tags-cell {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
  width: 100%;
  max-width: 320px;
}
.tags-more-row {
  display: none;
  justify-content: center;
  align-items: center;
  gap: 8px;
  width: 100%;
}
.tags-cell.has-overflow .tags-more-row {
  display: flex;
}
.tag-rows {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}
.tags-cell.has-overflow:not(.is-expanded) .tag-rows--expanded {
  display: none;
}
.tags-cell.has-overflow.is-expanded .tag-rows--collapsed {
  display: none;
}
.tag-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  align-items: center;
}
.tag-row--last:has(.tag-add) {
  grid-template-columns: repeat(4, minmax(0, 1fr)) auto;
}
.tag-row > .tag-pill:not(.tag-add):not(.tag-more) {
  min-width: 0;
  max-width: 100%;
  justify-self: stretch;
}
.tag-row > .tag-pill:not(.tag-add):not(.tag-more),
.tag-row > .tag-pill.tag-add {
  flex: unset;
}
.tag-pill.tag-more {
  cursor: pointer;
  color: var(--muted);
  padding: 2px 10px;
  font-size: 11px;
  flex: 0 0 auto;
}
.tag-pill.tag-more:hover { color: var(--text); border-color: var(--accent); }
.tags-cell .tag-pill.tag-add,
.tag-row > .tag-pill.tag-add {
  padding: 3px 10px;
  line-height: 1.2;
  color: var(--muted);
  cursor: pointer;
  justify-self: start;
  width: auto;
}
.tag-picker-chip {
  flex: 0 0 auto;
  width: auto;
  min-width: 0;
  min-height: 0;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 3px 10px;
  line-height: 1.2;
  font-size: 12px;
  font-weight: 600;
  background: #0f131c;
  color: var(--text);
  cursor: pointer;
}
.tag-picker-chip:hover { border-color: var(--accent); color: var(--accent); }
.tag-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 3px 10px;
  font-size: 12px;
  font-weight: 600;
  background: #141924;
  color: var(--text);
}
.tag-pill.tag-consent-blocked {
  border-color: #ef6b73;
  color: #ef6b73;
}
.tag-pill.tag-consent-allowed {
  border-color: #3cc087;
  color: #3cc087;
}
.tag-pill.tag-consent-none {
  border-color: var(--line);
  color: var(--muted);
}
.tag-body {
  cursor: pointer;
}
.tag-body:hover { opacity: 0.85; }
.tag-pill.tag-add {
  cursor: pointer;
  padding: 3px 12px;
  color: var(--muted);
}
.tag-pill.tag-add:hover { color: var(--text); border-color: var(--accent); }
.tag-remove {
  border: none;
  background: transparent;
  color: inherit;
  opacity: 0.65;
  cursor: pointer;
  padding: 0 2px;
  font-size: 14px;
  line-height: 1;
}
.tag-remove:hover { color: var(--bad); opacity: 1; }
.tag-picker {
  position: fixed;
  z-index: 40;
  background: #141924;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 10px;
  max-width: min(420px, calc(100vw - 24px));
  box-shadow: 0 8px 24px rgba(0,0,0,0.35);
}
.tag-picker-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  width: max-content;
  max-width: 100%;
}
.tag-picker-new {
  display: inline-flex;
  flex-wrap: nowrap;
  align-items: center;
  gap: 4px;
}
#tag_picker input[type=text] {
  width: 72px;
  min-width: 0;
  max-width: 120px;
  flex: 0 0 auto;
  height: 28px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: #0f131c;
  color: var(--text);
  padding: 0 10px;
  font-size: 12px;
}
#tag_picker .tag-picker-new button {
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #0f131c;
  color: var(--muted);
  width: 28px;
  height: 28px;
  flex-shrink: 0;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 0;
}
#tag_picker .tag-picker-new button:hover { border-color: var(--accent); color: var(--accent); }
.tag-picker-empty {
  font-size: 12px;
  color: var(--muted);
  margin-right: 4px;
}
.person-form-grid { display: grid; gap: 12px; }
.person-form-grid label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 4px; }
.person-form-grid input, .person-form-grid select {
  width: 100%;
  height: 38px;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: #141924;
  color: var(--text);
  padding: 0 10px;
}
.person-form-grid input[type=file] { height: auto; padding: 8px; }
.person-slug-preview { font-size: 12px; color: var(--muted); margin-top: 4px; }
.menu #server_state { margin-left: auto; }
.menu button {
  background: #262d3a;
  border: 1px solid #3a4457;
  height: 36px;
  padding: 0 14px;
  display: inline-flex;
  align-items: center;
}
.lang-toggle {
  display: inline-flex;
  align-items: center;
  margin-left: 4px;
}
.lang-toggle .lang-btn {
  margin: 0;
  background: #141924;
  border: 1px solid var(--line);
  color: var(--text);
  height: 36px;
  padding: 0 10px;
  border-radius: 9px;
  font-size: 12px;
  font-weight: 600;
  gap: 6px;
  white-space: nowrap;
}
.lang-toggle .lang-btn:hover { filter: brightness(1.08); }
.lang-toggle .lang-flag { font-size: 14px; line-height: 1; }
fieldset {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  margin-bottom: 14px;
  padding: 12px;
}
legend {
  color: var(--text);
  padding: 0 8px;
  font-weight: 600;
}
input[type=text], select {
  background: #11151c;
  color: var(--text);
  border: 1px solid #2f3747;
  border-radius: 8px;
  padding: 8px 10px;
  min-height: 36px;
}
input[type=text] { width: 560px; max-width: 100%; }
/* Make folder pick inputs responsive to reduce wrapping. */
#search_folder, #people_root_people {
  width: auto;
  flex: 1 1 360px;
  min-width: 280px;
  max-width: 760px;
  max-width: 100%;
}
select { min-width: 120px; }
label { margin-right: 12px; color: var(--text); }
.row { margin-bottom: 10px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
#analyze_form .form-row {
  display: grid;
  grid-template-columns: 6.5rem 1fr;
  column-gap: 12px;
  align-items: center;
  margin-bottom: 10px;
}
#analyze_form .form-row:last-of-type { margin-bottom: 0; }
#analyze_form .form-label { color: var(--text); line-height: 1.35; }
#analyze_form .form-control {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-width: 0;
  width: 100%;
}
#analyze_form .input-with-clear {
  position: relative;
  flex: 1 1 240px;
  min-width: 0;
  display: flex;
  align-items: center;
}
#analyze_form .input-with-clear input[type=text] {
  width: 100%;
  flex: 1 1 auto;
  padding-right: 36px;
}
#analyze_form .input-clear {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  width: 28px;
  height: 28px;
  min-height: 28px;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
#analyze_form .input-clear:hover {
  color: var(--text);
  background: #2a3344;
  filter: none;
}
.grid-two { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.group-title { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .4px; margin: 2px 0 8px; }
.status-grid { display:grid; grid-template-columns: repeat(3, minmax(120px, 1fr)); gap: 10px; margin-top: 6px; }
.metric { background:#121721; border:1px solid var(--line); border-radius:10px; padding:10px; }
.metric .k { color: var(--muted); font-size: 12px; }
.metric .v { font-weight: 700; margin-top: 2px; font-size: 16px; }
.badge { display:inline-block; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:700; border:1px solid var(--line); }
.badge.idle { color: var(--muted); }
.badge.running { color: #fff; background: #2e4f8f; border-color: #4165ab; }
.badge.done { color: #d8ffe8; background: #1f5d42; border-color: #2d7a58; }
.badge.warn { color: #fff4de; background: #6b4c21; border-color: #8a6230; }
.badge.fail { color: #ffe1e4; background: #6f2830; border-color: #8e3640; }
.app-version {
  position: fixed;
  right: 14px;
  bottom: 12px;
  z-index: 50;
  color: var(--muted);
  font-size: 11px;
  letter-spacing: 0.02em;
  opacity: 0.75;
  pointer-events: none;
  user-select: none;
}
button {
  background: var(--accent);
  border: none;
  border-radius: 9px;
  color: #f5f9ff;
  font-weight: 600;
  padding: 9px 14px;
  cursor: pointer;
  white-space: nowrap;
}
button:hover { filter: brightness(1.08); }
button.btn-stop { background: #c23b4a; }
button.btn-stop:hover { filter: brightness(1.08); background: #d44858; }
button.btn-small {
  padding: 4px 10px;
  font-size: 12px;
  border-radius: 7px;
  font-weight: 600;
  vertical-align: middle;
}
button.btn-muted {
  background: #2a3344;
  color: #d7deeb;
}
pre {
  margin: 0;
  background: #0a0d13;
  color: #dce3f3;
  border: 1px solid #293142;
  border-radius: 10px;
  padding: 12px;
  height: 250px;
  overflow-y: auto;
  overflow-x: hidden;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.45;
}
#activity {
  height: 220px;
}
#runstate {
  margin-top: 8px;
  color: var(--good);
  font-weight: 600;
}
details { border:1px solid var(--line); border-radius:10px; padding:8px 10px; background:#141922; }
summary { cursor:pointer; font-weight:600; color: var(--muted); }
a.person-link { color: var(--accent); text-decoration: none; font-weight: 600; cursor: pointer; }
a.person-link:hover { text-decoration: underline; }
.modal-backdrop {
  display: none; position: fixed; inset: 0; z-index: 2000;
  background: rgba(0,0,0,.72); align-items: center; justify-content: center; padding: 24px;
}
.modal-backdrop.open { display: flex; }
.modal {
  background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  max-width: 960px; width: 100%; max-height: 90vh; overflow: auto; padding: 16px;
}
.modal h2 { margin: 0 0 10px; font-size: 20px; }
.modal .close-btn { float: right; background: #262d3a; border: 1px solid #3a4457; }
.gallery-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; margin-top: 12px;
}
.gallery-grid img {
  width: 100%; height: 140px; object-fit: cover; border-radius: 8px; border: 1px solid var(--line);
  cursor: pointer; background: #0a0d13;
}
.gallery-grid img.gallery-thumb:hover { border-color: var(--accent); }
.gallery-thumb-wrap {
  position: relative;
}
.gallery-thumb-wrap img.gallery-thumb {
  width: 100%; height: 140px; object-fit: cover; border-radius: 8px; border: 1px solid var(--line);
  cursor: pointer; background: #0a0d13; display: block;
}
.gallery-score-badge {
  position: absolute;
  left: 6px;
  bottom: 6px;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: rgba(10, 13, 19, 0.82);
  color: var(--text);
  border: 1px solid var(--line);
  pointer-events: none;
}
.gallery-hero {
  margin-top: 12px; text-align: center;
}
.gallery-hero img {
  max-width: 100%; max-height: 55vh; border-radius: 10px; border: 1px solid var(--line);
}
.gallery-source {
  color: var(--muted);
  font-size: 12px;
  text-align: center;
  word-break: break-word;
  margin: 6px 0 8px;
  line-height: 1.35;
}
.gallery-match {
  color: var(--muted);
  font-size: 12px;
  text-align: center;
  margin: 0 0 8px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.gallery-pos-row {
  margin: 8px 0;
  text-align: center;
  color: var(--muted);
  font-size: 13px;
}
.review-hero-wrap {
  position: relative; display: inline-block; max-width: 100%; margin-top: 12px;
}
.review-hero-wrap img {
  display: block; max-width: 100%; max-height: 55vh; border-radius: 10px; border: 1px solid var(--line);
}
.review-box-layer {
  position: absolute; left: 0; top: 0; width: 100%; height: 100%; pointer-events: none;
}
.face-box {
  position: absolute; border: 2px solid #f59e0b; box-sizing: border-box;
  pointer-events: auto; cursor: pointer; background: rgba(245,158,11,.08);
}
.face-box.selected { border-color: #ef4444; background: rgba(239,68,68,.12); }
.face-box-label {
  position: absolute; left: 0; top: -20px; font-size: 11px; font-weight: 600;
  color: #fff; background: rgba(0,0,0,.72); padding: 1px 5px; border-radius: 4px;
  white-space: nowrap; max-width: 160px; overflow: hidden; text-overflow: ellipsis;
}
.review-face-row {
  display: grid; grid-template-columns: 1fr minmax(220px, 280px); gap: 10px; align-items: start;
  padding: 10px 0; border-bottom: 1px solid var(--line);
}
.review-face-row.selected { background: rgba(245,158,11,.06); }
.review-face-actions { display: flex; flex-direction: column; gap: 6px; }
.person-picker {
  position: relative; width: 100%;
}
.person-picker-toggle {
  width: 100%; text-align: left; background: #12161f; border: 1px solid var(--line);
  border-radius: 8px; padding: 8px 10px; color: var(--text); cursor: pointer; font-size: 13px;
}
.person-picker-toggle:hover { border-color: var(--accent); }
.person-picker-panel {
  position: absolute; z-index: 40; left: 0; right: 0; top: calc(100% + 4px);
  background: #161b26; border: 1px solid var(--line); border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0,0,0,.45); max-height: 280px; display: flex; flex-direction: column;
}
.person-picker-panel[hidden] {
  display: none !important;
}
.person-picker-search {
  margin: 8px; width: calc(100% - 16px); box-sizing: border-box;
  background: #0f131a; border: 1px solid var(--line); border-radius: 6px;
  padding: 7px 9px; color: var(--text); font-size: 13px;
}
.person-picker-list {
  overflow: auto; flex: 1; padding: 0 4px 4px;
}
.person-picker-item {
  display: block; width: 100%; text-align: left; background: transparent; border: 0;
  color: var(--text); padding: 7px 10px; border-radius: 6px; cursor: pointer; font-size: 13px;
}
.person-picker-item:hover, .person-picker-item.active { background: rgba(245,158,11,.12); }
.person-picker-item .hint { color: var(--muted); font-size: 11px; margin-left: 6px; }
.person-picker-footer {
  border-top: 1px solid var(--line); padding: 6px;
}
.person-picker-footer button {
  width: 100%; background: #1a2030; border: 1px solid var(--line); border-radius: 6px;
  padding: 7px 10px; color: var(--text); cursor: pointer; font-size: 12px; font-weight: 600;
}
.btn-add-folder {
  background: #1a2030; border: 1px solid var(--line); border-radius: 8px;
  padding: 6px 10px; font-size: 12px; cursor: pointer; color: var(--text);
}
.btn-add-folder:hover { border-color: var(--accent); }
.review-table tbody tr { cursor: pointer; }
.review-table tbody tr:hover { background: rgba(255,255,255,.03); }
.review-status-tabs {
  display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 12px;
}
.review-tab {
  background: #1a2030; border: 1px solid var(--line); border-radius: 999px;
  padding: 8px 14px; font-size: 13px; font-weight: 600; cursor: pointer; color: var(--text);
}
.review-tab:hover { border-color: var(--accent); }
.review-tab.active {
  background: #2e4f8f; border-color: #4165ab; color: #fff;
}
.review-tab .tab-count { color: var(--muted); font-weight: 500; margin-left: 4px; }
.review-tab.active .tab-count { color: rgba(255,255,255,.85); }
.review-nav-row {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px;
}
.review-nav-row .review-pos { color: var(--muted); font-size: 13px; margin-left: auto; }
/* Settings page form grid */
.settings-page fieldset {
  padding: var(--section-padding);
  margin-bottom: var(--section-gap);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
}
.settings-page .form-row {
  display: grid;
  grid-template-columns: var(--label-col-width) 1fr;
  column-gap: 16px;
  align-items: center;
  margin-bottom: var(--row-gap);
}
.settings-page .form-row:last-child { margin-bottom: 0; }
.settings-page .form-label {
  text-align: left;
  color: var(--text);
  line-height: 1.35;
}
.settings-page .form-control {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-width: 0;
  width: 100%;
}
.settings-page .form-control.muted { color: var(--muted); }
.settings-page input[type=text],
.settings-page select,
.settings-page button {
  height: var(--control-height);
  min-height: var(--control-height);
  box-sizing: border-box;
  border-radius: 8px;
  padding: 0 12px;
}
.settings-page input[type=text] {
  flex: 1 1 240px;
  width: auto;
  min-width: 0;
  max-width: 100%;
  background: #11151c;
  color: var(--text);
  border: 1px solid #2f3747;
}
.settings-page select.control-select,
.settings-page .control-select {
  width: var(--dropdown-width);
  min-width: var(--dropdown-width);
  max-width: var(--dropdown-width);
  background: #11151c;
  color: var(--text);
  border: 1px solid #2f3747;
}
.settings-page select.control-select-fit,
.settings-page .control-select-fit {
  width: fit-content;
  min-width: 180px;
  max-width: 100%;
}
.settings-page .btn-row-actions {
  display: flex;
  gap: 8px;
  margin-left: auto;
  flex-shrink: 0;
}
.settings-page .checkbox-grid {
  display: grid;
  grid-template-columns: repeat(2, var(--checkbox-col-width));
  gap: 12px 24px;
  margin-bottom: var(--row-gap);
  align-items: center;
}
.settings-page .checkbox-grid:last-child { margin-bottom: 0; }
.settings-page .checkbox-grid label {
  display: inline-flex;
  align-items: flex-start;
  gap: var(--checkbox-label-gap);
  margin-right: 0;
  color: var(--text);
}
.settings-page .label-with-help {
  display: inline;
  line-height: 1.35;
  min-width: 0;
}
.settings-page .form-label .label-with-help {
  display: inline;
}
.settings-page .checkbox-grid input[type=checkbox] {
  width: 16px;
  height: 16px;
  margin: 0;
  flex-shrink: 0;
}
.settings-page .form-hint {
  margin: 12px 0 0 calc(var(--label-col-width) + 16px);
  color: var(--muted);
  font-size: 13px;
  line-height: 1.45;
}
.settings-page .form-hint:first-child { margin-top: 0; }
.settings-page #db_test_result {
  display: block;
  margin: 0 0 var(--row-gap) calc(var(--label-col-width) + 16px);
  color: var(--muted);
  font-size: 13px;
  min-height: 1.2em;
}
.settings-page .group-title {
  margin: 4px 0 12px;
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.settings-page .settings-sub {
  margin-bottom: 28px;
}
.settings-page .settings-sub:last-child {
  margin-bottom: 0;
}
.settings-page .sub-hint {
  margin: 8px 0 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.45;
  opacity: 0.85;
  max-width: 760px;
  white-space: pre-line;
}
.settings-page .settings-ai-intro {
  margin: 0 0 18px;
  padding: 12px 14px;
  max-width: 760px;
  background: #11151c;
  border: 1px solid var(--line);
  border-radius: 8px;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.5;
}
.settings-page .settings-ai-intro p { margin: 0 0 8px; }
.settings-page .settings-ai-intro p:last-child { margin-bottom: 0; }
.settings-page .settings-ai-intro ul {
  margin: 4px 0 8px;
  padding-left: 18px;
}
.settings-page .settings-ai-intro li { margin: 2px 0; }
.settings-page .settings-ai-intro strong { color: var(--text); font-weight: 600; }
.settings-page .export-suboptions {
  margin-top: 10px;
  padding-left: 24px;
}
.settings-page .ingest-suboptions {
  margin-top: 10px;
  padding-left: 24px;
}
.settings-page .ingest-suboptions.is-disabled {
  opacity: 0.45;
  pointer-events: none;
}
.settings-page .export-suboptions .checkbox-grid {
  margin-bottom: 0;
}
.settings-page .input-with-clear {
  position: relative;
  flex: 1 1 240px;
  min-width: 0;
  display: flex;
  align-items: center;
}
.settings-page .input-with-clear input[type=text] {
  width: 100%;
  flex: 1 1 auto;
  padding-right: 36px;
}
.settings-page .input-clear {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  width: 28px;
  height: 28px;
  min-height: 28px;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.settings-page .input-clear:hover {
  color: var(--text);
  background: #2a3344;
  filter: none;
}
.settings-page #lightroom_fields.is-disabled {
  opacity: 0.45;
  pointer-events: none;
}
.settings-page #lightroom_fields.is-disabled input,
.settings-page #lightroom_fields.is-disabled select,
.settings-page #lightroom_fields.is-disabled button {
  cursor: not-allowed;
}
.settings-page details.form-advanced {
  margin-top: var(--row-gap);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 12px 14px;
  background: #141922;
}
.settings-page details.form-advanced > summary {
  cursor: pointer;
  font-weight: 600;
  color: var(--muted);
  list-style-position: outside;
}
.settings-page details.form-advanced[open] > summary {
  margin-bottom: var(--row-gap);
}
.settings-page details.form-advanced .form-row:last-child {
  margin-bottom: 0;
}
.settings-page details.form-advanced pre {
  height: auto;
  max-height: 280px;
  width: 100%;
}
.settings-page .settings-save {
  display: inline-block;
  width: 180px;
  margin-top: var(--section-gap);
  height: var(--control-height);
}
@media (max-width: 900px) {
  .grid-two { grid-template-columns: 1fr; }
  .settings-page .form-row {
    grid-template-columns: 1fr;
    gap: 8px;
  }
  .settings-page .form-hint,
  .settings-page #db_test_result {
    margin-left: 0;
  }
  .settings-page .checkbox-grid {
    grid-template-columns: 1fr;
  }
}
</style>"""

    def _lang(self) -> str:
        return lang_from_cookie_header(self.headers.get("Cookie"))

    def _header(self, active: str, subtitle_key: str, *, people_mismatch: bool | None = None) -> str:
        lang = self._lang()  # type: ignore[assignment]
        analyze_cls = "active" if active in ("analyze", "batch") else ""
        review_cls = "active" if active == "review" else ""
        settings_cls = "active" if active == "settings" else ""
        people_cls = "active" if active == "people" else ""
        if people_mismatch is None:
            with STATE.lock:
                people_mismatch = bool(STATE.people_has_mismatch)
        people_link = _nav_people_link_html(
            active_cls=people_cls,
            mismatch=people_mismatch,
            label=_t("nav.people", lang),
            mismatch_tip=_t("nav.people_mismatch_tip", lang),
        )
        version_label = html.escape(_t("app.version_label", lang, version=APP_VERSION))
        version_tip = html.escape(_t("app.version_tip", lang))
        subtitle = html.escape(_t(subtitle_key, lang))
        if lang == "de":
            lang_flag, lang_code, lang_title, next_lang = "🇩🇪", "DE", "Deutsch — klicken für English", "en"
        else:
            lang_flag, lang_code, lang_title, next_lang = "🇬🇧", "EN", "English — click for Deutsch", "de"
        return f"""
<div class="wrap">
<div class="app-version" title="{version_tip}">{version_label}</div>
<h1>{html.escape(_t("app.heading", lang))}</h1>
<div class="subtitle">{subtitle}</div>
<nav class="menu">
  <a class="{analyze_cls}" href="/">{html.escape(_t("nav.analyze", lang))}</a>
  <a class="{review_cls}" href="/review">{html.escape(_t("nav.review", lang))}</a>
  {people_link}
  <a class="{settings_cls}" href="/settings">{html.escape(_t("nav.settings", lang))}</a>
  <span id="server_state" class="badge idle">{html.escape(_t("nav.badge.idle", lang))}</span>
  <div class="lang-toggle">
    <button type="button" class="lang-btn" onclick="setLang('{next_lang}')" title="{html.escape(lang_title)}" aria-label="{html.escape(_t("nav.lang_aria", lang))}">
      <span class="lang-flag" aria-hidden="true">{lang_flag}</span> {lang_code}
    </button>
  </div>
  <button type="button" onclick="stopServer()">{html.escape(_t("nav.stop_server", lang))}</button>
</nav>
{i18n_bootstrap_script(lang)}
<script>
function setLang(code) {{
  var v = (code === 'de') ? 'de' : 'en';
  document.cookie = '{LANG_COOKIE}=' + v + '; path=/; max-age=31536000; SameSite=Lax';
  try {{ localStorage.setItem('{LANG_COOKIE}', v); }} catch (e) {{}}
  location.reload();
}}
</script>
<script>
{_folder_picker_client_js()}
</script>
"""

    def _send_html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, obj: dict[str, object], status: int = 200) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _parse_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {k: (v[-1] if v else "") for k, v in parsed.items()}

    def _parse_multipart(self) -> MultipartForm:
        length = int(self.headers.get("Content-Length", "0") or "0")
        ctype = self.headers.get("Content-Type", "")
        body = self.rfile.read(length)
        return parse_multipart(body, ctype)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/favicon.ico", "/favicon.png"):
            if not _FAVICON_PATH.is_file():
                self._send_html(html.escape(_t("common.not_found", self._lang())), 404)
                return
            data = _FAVICON_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/status":
            snap = STATE.snapshot()
            snap["active_runs"] = _get_active_runs_cached()
            snap["this_host"] = this_host()
            snap["db_identity"] = _db_identity()
            self._send_json(snap)
            return
        if parsed.path == "/api/pick_folder":
            qs = parse_qs(parsed.query)
            target = (qs.get("target", [""])[0] or "").strip()
            try:
                picked = _pick_folder(lang=self._lang(), target=target)
                self._send_json({"ok": True, "path": picked})
            except subprocess.CalledProcessError as e:
                self._send_json(
                    {"ok": False, "error": _t("api.picker_cancelled", self._lang(), code=e.returncode)}
                )
            except (OSError, subprocess.SubprocessError) as e:
                self._send_json({"ok": False, "error": str(e)})
            except Exception as e:
                logging.getLogger("faceit_ai").exception("pick_folder failed")
                self._send_json({"ok": False, "error": str(e)})
            return
        if parsed.path == "/api/test_db":
            qs = parse_qs(parsed.query)
            url = (qs.get("url", [""])[0] or "").strip()
            self._send_json(_test_db_connection(url, lang=self._lang()))
            return
        if parsed.path == "/api/person_photos":
            qs = parse_qs(parsed.query)
            name = (qs.get("name", [""])[0] or "").strip()
            paths = _list_person_gallery_paths(name)
            from faceit_ai.services.collected_photos import (
                lookup_sources_by_collected_paths,
                resolve_match_score_for_collected,
            )
            from faceit_ai.services.processing_runs import folder_claim_key

            sources_by_key: dict = {}
            scores_by_key: dict[str, float | None] = {}
            try:
                settings = load_settings()
                _, session_factory = create_engine_and_session_factory(settings.database_url)
                with session_scope(session_factory) as session:
                    sources_by_key = lookup_sources_by_collected_paths(session, paths)
                    for key, row in sources_by_key.items():
                        scores_by_key[key] = resolve_match_score_for_collected(session, row)
            except Exception as e:
                STATE.add_log(f"[warn] could not load collected photo sources: {e}")

            photos: list[dict[str, object]] = []
            for p in paths:
                key = folder_claim_key(p)
                row = sources_by_key.get(key)
                source_path = str(row.source_path) if row is not None else None
                match_score = scores_by_key.get(key)
                photos.append(
                    {
                        "path": str(p),
                        "name": p.name,
                        "url": "/api/person_photo?path=" + quote(str(p)),
                        "source_path": source_path,
                        "source_name": Path(source_path).name if source_path else None,
                        "match_score": match_score,
                    }
                )
            self._send_json(
                {
                    "ok": True,
                    "name": name,
                    "count": len(photos),
                    "photos": photos,
                }
            )
            return
        if parsed.path == "/api/person_photo":
            qs = parse_qs(parsed.query)
            path_text = (qs.get("path", [""])[0] or "").strip()
            file_path = _safe_gallery_file(path_text)
            if file_path is None:
                self._send_html(html.escape(_t("common.not_found", self._lang())), 404)
                return
            mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/settings":
            lang = self._lang()
            cfg = _read_config_form()
            body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(_t("app.title.settings", lang))}</title>
{self._base_style()}
</head><body>
{self._header("settings", "subtitle.settings")}
<form class="settings-page" method="post" action="/save_settings">
<fieldset><legend>{html.escape(_t("settings.section.data", lang))}</legend>
<div class="form-row">
  <span class="form-label"><span class="badge idle">{html.escape(_t("settings.current_db", lang))}</span></span>
  <span class="form-control muted">{html.escape(str(cfg['current_db']))}</span>
</div>
<div class="form-row">
  <span class="form-label">{_label_with_help(_t("settings.data_folder", lang), _t("settings.data_folder_help", lang))}</span>
  <div class="form-control">
    <div class="input-with-clear">
      <input id="data_dir" type="text" name="data_dir" placeholder="{html.escape(_t("settings.data_folder_placeholder", lang))}" value="{html.escape(str(cfg['data_dir']))}">
      <button type="button" class="input-clear" data-clear-for="data_dir" aria-label="{html.escape(_t("common.clear", lang))}" hidden>&times;</button>
    </div>
    <div class="btn-row-actions"><button type="button" onclick="pickFolder('data_dir')">{html.escape(_t("common.choose_finder", lang))}</button></div>
  </div>
</div>
<div class="form-row">
  <span class="form-label">{_label_with_help(_t("settings.database_url", lang), _t("settings.database_url_help", lang))}</span>
  <div class="form-control">
    <div class="input-with-clear">
      <input id="database_url" type="text" name="database_url" placeholder="{html.escape(_t("settings.database_url_placeholder", lang))}" value="{html.escape(str(cfg['database_url']))}">
      <button type="button" class="input-clear" data-clear-for="database_url" aria-label="{html.escape(_t("common.clear", lang))}" hidden>&times;</button>
    </div>
    <div class="btn-row-actions"><button type="button" onclick="testDb()">{html.escape(_t("settings.btn.test_db", lang))}</button></div>
  </div>
</div>
<span id="db_test_result"></span>
<p class="form-hint">{html.escape(_t("settings.db.hint", lang))}</p>
</fieldset>
<fieldset><legend>{html.escape(_t("settings.section.analyze", lang))}</legend>
<div class="settings-sub">
  <div class="group-title">{html.escape(_t("settings.group.reanalysis", lang))}</div>
  <div class="checkbox-grid">
    <label><input type="checkbox" name="force_default" {'checked' if cfg['force_default'] else ''}>{_label_with_help(_t("settings.force_reanalyze", lang), _t("settings.force_reanalyze_help", lang))}</label>
  </div>
  <p class="sub-hint">{html.escape(_t("settings.force_reanalyze_hint", lang))}</p>
</div>
<div class="settings-sub">
  <div class="group-title">{html.escape(_t("settings.group.archive", lang))}</div>
  <div class="checkbox-grid">
    <label><input id="ingest_enabled" type="checkbox" name="ingest_enabled" {'checked' if cfg['ingest_enabled'] else ''} onchange="toggleIngestOrder()">{_label_with_help(_t("settings.ingest_enable", lang), _t("settings.ingest_enable_help", lang))}</label>
  </div>
  <div id="ingest_suboptions" class="ingest-suboptions{'' if cfg['ingest_enabled'] else ' is-disabled'}">
    <div class="form-row">
      <span class="form-label">{_label_with_help(_t("settings.ingest_order", lang), _t("settings.ingest_order_help", lang))}</span>
      <div class="form-control">
        <select class="control-select control-select-fit" name="ingest_order">
          <option value="copy_then_analyze" {'selected' if cfg.get('ingest_order','copy_then_analyze')=='copy_then_analyze' else ''}>{html.escape(_t("settings.ingest_order.copy_first", lang))}</option>
          <option value="analyze_then_copy" {'selected' if cfg.get('ingest_order')=='analyze_then_copy' else ''}>{html.escape(_t("settings.ingest_order.analyze_first", lang))}</option>
        </select>
      </div>
    </div>
    <p class="sub-hint">{html.escape(_t("settings.ingest_order_hint", lang))}</p>
  </div>
</div>
<div class="settings-sub">
  <div class="group-title">{html.escape(_t("settings.group.flagged", lang))}</div>
  <div class="form-row">
    <span class="form-label">{_label_with_help(_t("settings.export_flagged", lang), _t("settings.export_flagged_help", lang))}</span>
    <div class="form-control">
      <select class="control-select" name="export_flagged">
        <option value="off" {'selected' if cfg['export_flagged']=='off' else ''}>{html.escape(_t("settings.export.off", lang))}</option>
        <option value="copy" {'selected' if cfg['export_flagged']=='copy' else ''}>{html.escape(_t("settings.export.copy", lang))}</option>
        <option value="move" {'selected' if cfg['export_flagged']=='move' else ''}>{html.escape(_t("settings.export.move", lang))}</option>
      </select>
    </div>
  </div>
  <div class="export-suboptions">
    <div class="checkbox-grid">
      <label><input type="checkbox" name="export_status_blocked" {'checked' if cfg['export_status_blocked'] else ''}>{_label_with_help(_t("settings.export.blocked", lang), _t("settings.export.blocked_help", lang))}</label>
      <label><input type="checkbox" name="export_status_review" {'checked' if cfg['export_status_review'] else ''}>{_label_with_help(_t("settings.export.review", lang), _t("settings.export.review_help", lang))}</label>
    </div>
    <p class="sub-hint">{html.escape(_t("settings.export_hint", lang))}</p>
  </div>
</div>
<div class="settings-sub">
  <div class="group-title">{html.escape(_t("settings.group.people_folder", lang))}</div>
  <div class="checkbox-grid">
    <label><input type="checkbox" name="collect_crop_portrait" {'checked' if cfg['collect_crop_portrait'] else ''}>{_label_with_help(_t("settings.crop_portraits", lang), _t("settings.crop_portraits_help", lang))}</label>
  </div>
  <p class="sub-hint">{html.escape(_t("settings.crop_portraits_hint", lang))}</p>
</div>
</fieldset>
<fieldset><legend>{html.escape(_t("settings.section.lightroom", lang))}</legend>
<div class="checkbox-grid">
  <label><input id="sync_metadata_default" type="checkbox" name="sync_metadata_default" {'checked' if cfg['sync_metadata_default'] else ''} onchange="toggleLightroomFields()">{_label_with_help(_t("settings.lr.enable", lang), _t("settings.lr.enable_help", lang))}</label>
</div>
<div id="lightroom_fields">
  <div class="group-title">{html.escape(_t("settings.lr.labels_group", lang))}</div>
  <div class="form-row">
    <span class="form-label">{_label_with_help(_t("settings.lr.blocked_label", lang), _t("settings.lr.blocked_label_help", lang))}</span>
    <div class="form-control">
      <select class="control-select" name="label_blocked"><option {'selected' if cfg['label_blocked']=='Rot' else ''}>Rot</option><option {'selected' if cfg['label_blocked']=='Gelb' else ''}>Gelb</option><option {'selected' if cfg['label_blocked']=='Grün' else ''}>Grün</option><option {'selected' if cfg['label_blocked']=='Blau' else ''}>Blau</option><option {'selected' if cfg['label_blocked']=='Lila' else ''}>Lila</option><option {'selected' if cfg['label_blocked']=='None' else ''}>None</option></select>
    </div>
  </div>
  <div class="form-row">
    <span class="form-label">{_label_with_help(_t("settings.lr.review_label", lang), _t("settings.lr.review_label_help", lang))}</span>
    <div class="form-control">
      <select class="control-select" name="label_review"><option {'selected' if cfg['label_review']=='Rot' else ''}>Rot</option><option {'selected' if cfg['label_review']=='Gelb' else ''}>Gelb</option><option {'selected' if cfg['label_review']=='Grün' else ''}>Grün</option><option {'selected' if cfg['label_review']=='Blau' else ''}>Blau</option><option {'selected' if cfg['label_review']=='Lila' else ''}>Lila</option><option {'selected' if cfg['label_review']=='None' else ''}>None</option></select>
    </div>
  </div>
  <div class="form-row">
    <span class="form-label">{_label_with_help(_t("settings.lr.ok_label", lang), _t("settings.lr.ok_label_help", lang))}</span>
    <div class="form-control">
      <select class="control-select" name="label_ok"><option {'selected' if cfg['label_ok']=='Rot' else ''}>Rot</option><option {'selected' if cfg['label_ok']=='Gelb' else ''}>Gelb</option><option {'selected' if cfg['label_ok']=='Grün' else ''}>Grün</option><option {'selected' if cfg['label_ok']=='Blau' else ''}>Blau</option><option {'selected' if cfg['label_ok']=='Lila' else ''}>Lila</option><option {'selected' if cfg['label_ok']=='None' else ''}>None</option></select>
    </div>
  </div>
  <details class="form-advanced">
    <summary>{html.escape(_t("settings.advanced", lang))}</summary>
    <div class="checkbox-grid">
      <label><input type="checkbox" name="verify_after_write" {'checked' if cfg['verify_after_write'] else ''}>{_label_with_help(_t("settings.lr.verify", lang), _t("settings.lr.verify_help", lang))}</label>
    </div>
    <div class="form-row">
      <span class="form-label">{html.escape(_t("settings.lr.exiftool_path", lang))}</span>
      <div class="form-control">
        <input type="text" name="exiftool_path" value="{html.escape(str(cfg['exiftool_path']))}">
      </div>
    </div>
  </details>
</div>
</fieldset>
<fieldset><legend>{html.escape(_t("settings.section.ai", lang))}</legend>
{_settings_ai_intro_html(lang)}
<div class="form-row">
  <span class="form-label">{_label_with_help(_t("settings.ai.raw_decode", lang), _t("settings.ai.raw_decode_help", lang))}</span>
  <div class="form-control">
    <select class="control-select control-select-fit" name="raw_decode_size">
      <option value="full" {'selected' if cfg.get('raw_decode_size')=='full' else ''}>{html.escape(_t("settings.ai.raw_decode.full", lang))}</option>
      <option value="half" {'selected' if cfg.get('raw_decode_size','half')=='half' else ''}>{html.escape(_t("settings.ai.raw_decode.half", lang))}</option>
      <option value="quarter" {'selected' if cfg.get('raw_decode_size')=='quarter' else ''}>{html.escape(_t("settings.ai.raw_decode.quarter", lang))}</option>
    </select>
  </div>
</div>
<p class="sub-hint">{html.escape(_t("settings.ai.raw_decode_hint", lang))}</p>
<div class="form-row">
  <span class="form-label">{html.escape(_t("settings.ai.max_dimension", lang))}{_help_mark(_t("settings.ai.max_dimension_help", lang))}</span>
  <div class="form-control">
    <input type="text" name="max_dimension" value="{html.escape(str(cfg['max_dimension']))}">
  </div>
</div>
<div class="form-row">
  <span class="form-label">{html.escape(_t("settings.ai.det_size", lang))}{_help_mark(_t("settings.ai.det_size_help", lang))}</span>
  <div class="form-control">
    <input type="text" name="det_size" value="{html.escape(str(cfg['det_size']))}">
  </div>
</div>
<div class="form-row">
  <span class="form-label">{html.escape(_t("settings.ai.providers", lang))}{_help_mark(_t("settings.ai.providers_help", lang))}</span>
  <div class="form-control">
    <input type="text" name="inference_providers" placeholder="{html.escape(_t("settings.ai.providers_placeholder", lang))}" value="{html.escape(str(cfg.get('inference_providers', 'auto')))}">
  </div>
</div>
<details class="form-advanced">
  <summary>{html.escape(_t("settings.advanced", lang))}</summary>
  <div class="checkbox-grid">
    <label><input type="checkbox" name="debug_logging" {'checked' if cfg['debug_logging'] else ''}>{html.escape(_t("settings.ai.debug", lang))}</label>
  </div>
  <div class="form-row">
    <span class="form-label">{html.escape(_t("settings.ai.config_preview", lang))}</span>
    <div class="form-control">
      <pre>{html.escape(str(cfg['yaml_preview']))}</pre>
    </div>
  </div>
</details>
</fieldset>
<button class="settings-save" type="submit">{html.escape(_t("settings.btn.save", lang))}</button>
</form>
</div>
<script>
async function testDb() {{
  const el = document.getElementById('db_test_result');
  const urlEl = document.getElementById('database_url');
  const url = urlEl.value || '';
  try {{ localStorage.setItem('faceit_database_url', url); }} catch (e) {{}}
  el.style.color = '#9aa4b2';
  el.textContent = t('settings.db.testing');
  try {{
    const r = await fetch('/api/test_db?url=' + encodeURIComponent(url));
    const d = await r.json();
    if (d.ok) {{
      el.style.color = '#3cc087';
      el.textContent = t('settings.db.ok', {{backend: (d.backend || 'database')}});
    }} else {{
      el.style.color = '#ef6b73';
      el.textContent = t('settings.db.failed', {{error: (d.error || 'could not connect')}});
    }}
  }} catch (e) {{
    el.style.color = '#ef6b73';
    el.textContent = t('settings.db.failed', {{error: e}});
  }}
}}

function toggleIngestOrder() {{
  const master = document.getElementById('ingest_enabled');
  const wrap = document.getElementById('ingest_suboptions');
  if (!master || !wrap) return;
  wrap.classList.toggle('is-disabled', !master.checked);
}}

function toggleLightroomFields() {{
  const master = document.getElementById('sync_metadata_default');
  const wrap = document.getElementById('lightroom_fields');
  if (!master || !wrap) return;
  // Visual grey-out only — do not set disabled (those fields would be omitted on save).
  wrap.classList.toggle('is-disabled', !master.checked);
}}

function syncClearButton(inputId) {{
  const input = document.getElementById(inputId);
  const btn = document.querySelector('.input-clear[data-clear-for="' + inputId + '"]');
  if (!input || !btn) return;
  btn.hidden = !(input.value || '').trim();
}}

async function stopServer() {{
  if (!confirm(t('common.confirm.stop_server'))) return;
  const r = await fetch('/shutdown', {{ method: 'POST' }});
  const d = await r.json();
  const msg = d.message || t('common.shutdown.fallback_msg');
  document.body.innerHTML = `
    <div style="display:flex;min-height:100vh;align-items:center;justify-content:center;background:#0f1115;color:#e7ebf3;font-family:Inter,-apple-system,sans-serif;">
      <div style="max-width:520px;padding:24px;border:1px solid #2a3140;border-radius:12px;background:#171a21;">
        <h2 style="margin:0 0 10px;">${{t('common.shutdown.title')}}</h2>
        <p style="margin:0 0 12px;color:#9aa4b2;">${{msg}}</p>
        <p style="margin:0;color:#9aa4b2;">${{t('common.shutdown.close_tab')}}</p>
      </div>
    </div>
  `;
}}

(function() {{
  const ids = ['database_url', 'data_dir'];
  for (const id of ids) {{
    const el = document.getElementById(id);
    if (!el) continue;
    try {{
      const saved = localStorage.getItem('faceit_' + id);
      if (saved && !el.value) el.value = saved;
    }} catch (e) {{}}
    el.addEventListener('input', function() {{
      try {{ localStorage.setItem('faceit_' + id, el.value); }} catch (e) {{}}
      syncClearButton(id);
    }});
    syncClearButton(id);
  }}
  document.querySelectorAll('.input-clear[data-clear-for]').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      const id = btn.getAttribute('data-clear-for');
      const el = document.getElementById(id);
      if (!el) return;
      el.value = '';
      try {{ localStorage.setItem('faceit_' + id, ''); }} catch (e) {{}}
      syncClearButton(id);
      el.dispatchEvent(new Event('input', {{ bubbles: true }}));
      el.focus();
    }});
  }});
  toggleLightroomFields();
  toggleIngestOrder();
}})();
</script>
</body></html>"""
            self._send_html(body)
            return
        if parsed.path == "/api/people_table":
            lang = self._lang()
            rows = _list_people_rows()
            self._send_json(
                {
                    "ok": True,
                    "html": _people_table_body_html(rows, lang=lang),
                    "has_mismatch": _people_mismatch_warn_visible(rows),
                    "all_tags": _collect_all_people_tags(rows),
                }
            )
            return
        if parsed.path == "/api/review_photos":
            qs = parse_qs(parsed.query)
            folder = (qs.get("folder", [""])[0] or "").strip()
            status = _parse_review_status((qs.get("status", ["review"])[0] or "review"))
            self._send_json(_review_photos_response(folder, status, lang=self._lang()))
            return
        if parsed.path == "/api/review_photo/preview":
            qs = parse_qs(parsed.query)
            folder = (qs.get("folder", [""])[0] or "").strip()
            status = _parse_review_status((qs.get("status", ["review"])[0] or "review"))
            try:
                asset_id = int((qs.get("id", ["0"])[0] or "0").strip())
            except ValueError:
                self._send_html(html.escape(_t("common.bad_request", self._lang())), 400)
                return
            folder_path = _safe_review_folder(folder)
            if folder_path is None or asset_id <= 0:
                self._send_html(html.escape(_t("common.not_found", self._lang())), 404)
                return
            settings = load_settings()
            _, session_factory = create_engine_and_session_factory(settings.database_url)
            with session_scope(session_factory) as session:
                data = render_review_preview_jpeg(
                    session,
                    asset_id,
                    folder_path,
                    image_cfg=settings.pipeline.image,
                    status=status,  # type: ignore[arg-type]
                )
            if data is None:
                self._send_html(html.escape(_t("common.not_found", self._lang())), 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=60")
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/review_photo":
            qs = parse_qs(parsed.query)
            folder = (qs.get("folder", [""])[0] or "").strip()
            status = _parse_review_status((qs.get("status", ["review"])[0] or "review"))
            try:
                asset_id = int((qs.get("id", ["0"])[0] or "0").strip())
            except ValueError:
                self._send_json({"ok": False, "error": _t("api.invalid_asset", self._lang())})
                return
            self._send_json(_review_photo_detail(asset_id, folder, status, lang=self._lang()))
            return
        if parsed.path == "/review":
            lang = self._lang()
            qs = parse_qs(parsed.query)
            status = _parse_review_status((qs.get("status", ["review"])[0] or "review"))
            folder_q = (qs.get("folder", [""])[0] or "").strip()
            folder_prefill = folder_q or str(STATE.analyze_folder_last or "")
            folder_esc = html.escape(folder_prefill)
            status_esc = html.escape(status)
            body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(_t("app.title.review", lang))}</title>
{self._base_style()}
</head><body>
{self._header("review", "subtitle.review")}
<fieldset><legend>{html.escape(_t("review.folder.legend", lang))}</legend>
  <div class="row">
    <input id="review_folder" type="text" name="folder" placeholder="{html.escape(_t("review.folder.placeholder", lang))}" value="{folder_esc}">
    <button type="button" onclick="pickFolder('review_folder')">{html.escape(_t("common.choose_finder", lang))}</button>
    <button type="button" onclick="loadReviewList()">{html.escape(_t("review.btn.load", lang))}</button>
  </div>
  <div id="review_list_meta" style="color:var(--muted);font-size:13px;margin-top:8px;"></div>
</fieldset>

<div id="review_status_tabs" class="review-status-tabs" style="display:none;">
  <button type="button" id="review_tab_review" class="review-tab" onclick="switchReviewStatus('review')">{html.escape(_t("review.tab.review", lang))}<span class="tab-count" id="review_count_review"> (0)</span></button>
  <button type="button" id="review_tab_blocked" class="review-tab" onclick="switchReviewStatus('blocked')">{html.escape(_t("review.tab.blocked", lang))}<span class="tab-count" id="review_count_blocked"> (0)</span></button>
</div>

<div id="review_batch_row" style="display:none;margin:12px 0;">
  <button type="button" onclick="batchReviewBlocked()" style="background:#7b2432;border-radius:9px;">{html.escape(_t("review.btn.batch_blocked", lang))}</button>
  <span style="color:var(--muted);font-size:13px;margin-left:8px;">{html.escape(_t("review.batch.hint", lang))}</span>
</div>

<fieldset><legend id="review_gallery_legend">{html.escape(_t("review.gallery.legend", lang))}</legend>
  <div id="review_gallery" class="gallery-grid"></div>
  <div id="review_gallery_empty" style="color:var(--muted);font-size:13px;">{html.escape(_t("review.empty.choose", lang))}</div>
</fieldset>

<div id="review_modal" class="modal-backdrop" onclick="if(event.target===this) closeReviewModal()">
  <div class="modal" style="max-width:920px;">
    <button type="button" class="close-btn" onclick="closeReviewModal()">{html.escape(_t("common.close", lang))}</button>
    <h2 id="review_modal_title">{html.escape(_t("review.modal.title", lang))}</h2>
    <div id="review_modal_meta" style="color:var(--muted);font-size:13px;"></div>
    <div class="review-nav-row">
      <button type="button" id="review_prev_btn" onclick="reviewNav(-1)">{html.escape(_t("review.nav.prev", lang))}</button>
      <button type="button" id="review_next_btn" onclick="reviewNav(1)">{html.escape(_t("review.nav.next", lang))}</button>
      <span id="review_pos_label" class="review-pos"></span>
    </div>
    <div style="text-align:center;">
      <div class="review-hero-wrap" id="review_hero_wrap">
        <img id="review_preview" alt="{html.escape(_t("common.preview_alt", lang))}">
        <div id="review_boxes" class="review-box-layer"></div>
      </div>
    </div>
    <div id="review_faces_panel" style="margin-top:12px;"></div>
    <div style="margin-top:12px;display:flex;flex-wrap:wrap;gap:8px;">
      <button type="button" id="review_to_blocked_btn" onclick="confirmReviewBlocked()">{html.escape(_t("review.btn.move_blocked", lang))}</button>
      <button type="button" id="review_to_ok_btn" onclick="confirmReviewOk()">{html.escape(_t("review.btn.move_ok", lang))}</button>
    </div>
  </div>
</div>

<div id="review_add_person_modal" class="modal-backdrop" onclick="if(event.target===this) closeReviewAddPersonModal()">
  <div class="modal" style="max-width:480px;">
    <button type="button" class="close-btn" onclick="closeReviewAddPersonModal()">{html.escape(_t("common.close", lang))}</button>
    <h2>{html.escape(_t("review.person.add_title", lang))}</h2>
    <form id="review_add_person_form" onsubmit="return submitReviewAddPerson(event)">
      <div class="person-form-grid">
        <div><label for="review_add_first">{html.escape(_t("people.label.vorname", lang))}</label><input id="review_add_first" required></div>
        <div><label for="review_add_last">{html.escape(_t("people.label.nachname", lang))}</label><input id="review_add_last" required></div>
        <div><label for="review_add_consent">{html.escape(_t("people.label.consent", lang))}</label>
          <select id="review_add_consent"><option value="blocked" selected>{html.escape(_t("people.consent.blocked", lang))}</option><option value="allowed">{html.escape(_t("people.consent.allowed", lang))}</option></select>
        </div>
      </div>
      <div style="margin-top:14px;display:flex;gap:8px;">
        <button type="submit">{html.escape(_t("review.person.add_submit", lang))}</button>
      </div>
    </form>
  </div>
</div>

<script>
let reviewStatus = '{status_esc}';
let reviewFolder = '';
let reviewPhotos = [];
let reviewPhotoIndex = -1;
let reviewAssetId = 0;
let reviewDetail = null;
let reviewSelectedFaceId = null;

function escapeHtml(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function escapeHtmlAttr(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
}}

function reviewKindLabel() {{
  return t(reviewStatus === 'blocked' ? 'review.tab.blocked' : 'review.tab.review');
}}

async function loadReviewList(keepModal) {{
  const folder = (document.getElementById('review_folder') || {{}}).value || '';
  reviewFolder = folder.trim();
  const meta = document.getElementById('review_list_meta');
  const label = reviewKindLabel();
  if (!reviewFolder) {{
    if (meta) meta.textContent = t('review.meta.choose_folder');
    reviewPhotos = [];
    renderReviewGallery();
    return;
  }}
  if (meta) meta.textContent = t('review.meta.loading');
  try {{
    const r = await fetch('/api/review_photos?folder=' + encodeURIComponent(reviewFolder) + '&status=' + encodeURIComponent(reviewStatus));
    const d = await r.json();
    if (!d.ok) {{
      if (meta) meta.textContent = d.error || t('review.meta.load_failed', {{label: label}});
      return;
    }}
    reviewPhotos = d.photos || [];
    updateReviewTabs(d.status_counts || {{}});
    if (meta) meta.textContent = t('review.meta.count', {{n: d.count, kind: label}});
    renderReviewGallery();
    updateReviewUrl();
    if (keepModal && reviewPhotos.length && reviewPhotoIndex >= 0) {{
      const idx = Math.min(reviewPhotoIndex, reviewPhotos.length - 1);
      await openReviewAtIndex(idx);
    }} else if (!keepModal) {{
      reviewPhotoIndex = -1;
    }}
  }} catch (e) {{
    if (meta) meta.textContent = t('common.request_failed');
  }}
}}

function updateReviewTabs(counts) {{
  const tabs = document.getElementById('review_status_tabs');
  const cr = document.getElementById('review_count_review');
  const cb = document.getElementById('review_count_blocked');
  if (!tabs) return;
  tabs.style.display = 'flex';
  const rv = (counts && counts.review != null) ? counts.review : 0;
  const bl = (counts && counts.blocked != null) ? counts.blocked : 0;
  if (cr) cr.textContent = ' (' + rv + ')';
  if (cb) cb.textContent = ' (' + bl + ')';
  const tr = document.getElementById('review_tab_review');
  const tb = document.getElementById('review_tab_blocked');
  if (tr) tr.classList.toggle('active', reviewStatus === 'review');
  if (tb) tb.classList.toggle('active', reviewStatus === 'blocked');
  const batchRow = document.getElementById('review_batch_row');
  if (batchRow) batchRow.style.display = (reviewStatus === 'review' && rv > 0) ? 'block' : 'none';
}}

function updateReviewUrl() {{
  if (!reviewFolder) return;
  const u = new URL(window.location.href);
  u.searchParams.set('folder', reviewFolder);
  u.searchParams.set('status', reviewStatus);
  history.replaceState(null, '', u.pathname + u.search);
}}

function switchReviewStatus(next) {{
  if (next !== 'review' && next !== 'blocked') return;
  if (reviewStatus === next) return;
  reviewStatus = next;
  updateReviewUrl();
  loadReviewList(true);
}}

function renderReviewGallery() {{
  const grid = document.getElementById('review_gallery');
  const empty = document.getElementById('review_gallery_empty');
  const legend = document.getElementById('review_gallery_legend');
  if (!grid) return;
  const kind = reviewKindLabel();
  if (legend) legend.textContent = t('review.gallery.legend_dynamic', {{kind: kind}});
  if (!reviewPhotos.length) {{
    grid.innerHTML = '';
    if (empty) {{
      empty.style.display = 'block';
      empty.textContent = t('review.empty.none', {{kind: kind}});
    }}
    return;
  }}
  if (empty) empty.style.display = 'none';
  grid.innerHTML = reviewPhotos.map(function(p, idx) {{
    const url = escapeHtmlAttr(p.preview_url || '');
    const name = escapeHtmlAttr(p.name || '');
    const miss = p.missing_on_disk ? (' ' + t('review.thumb.missing')) : '';
    return (
      '<img class="gallery-thumb review-thumb" src="' + url + '" alt="' + name + '" title="' + name + miss + '" '
      + 'data-idx="' + idx + '" onclick="openReviewAtIndex(' + idx + ')">'
    );
  }}).join('');
}}

function highlightReviewThumb() {{
  document.querySelectorAll('#review_gallery img.review-thumb').forEach(function(el) {{
    const active = parseInt(el.dataset.idx || '-1', 10) === reviewPhotoIndex;
    el.style.borderColor = active ? 'var(--accent)' : '';
    el.style.boxShadow = active ? '0 0 0 2px var(--accent)' : '';
  }});
}}

function updateReviewNavUi() {{
  const prev = document.getElementById('review_prev_btn');
  const next = document.getElementById('review_next_btn');
  const pos = document.getElementById('review_pos_label');
  const toBlocked = document.getElementById('review_to_blocked_btn');
  const toOk = document.getElementById('review_to_ok_btn');
  const atStart = reviewPhotoIndex <= 0;
  const atEnd = reviewPhotoIndex < 0 || reviewPhotoIndex >= reviewPhotos.length - 1;
  if (prev) prev.disabled = atStart;
  if (next) next.disabled = atEnd;
  if (pos) {{
    pos.textContent = (reviewPhotos.length && reviewPhotoIndex >= 0)
      ? ((reviewPhotoIndex + 1) + ' / ' + reviewPhotos.length)
      : '';
  }}
  if (toBlocked) {{
    toBlocked.style.display = 'inline-block';
    toBlocked.style.background = '#7b2432';
    toBlocked.style.borderRadius = '9px';
    toBlocked.textContent = reviewStatus === 'blocked'
      ? t('review.btn.add_faces')
      : t('review.btn.move_blocked');
  }}
  if (toOk) {{
    toOk.style.display = 'inline-block';
    toOk.style.background = '#1f6f4a';
    toOk.style.borderRadius = '9px';
    toOk.textContent = reviewStatus === 'blocked' ? t('review.btn.move_ok') : t('review.btn.move_ok_unknown');
  }}
  highlightReviewThumb();
}}

function personLabel(slug) {{
  const labels = (reviewDetail && reviewDetail.people_labels) ? reviewDetail.people_labels : {{}};
  return labels[slug] || slug;
}}

let reviewFacePicks = {{}};
let reviewFacePicksInitial = {{}};
let reviewAddPersonFaceId = null;
let reviewOpenPickerFaceId = null;

function folderSlugSet() {{
  const entries = (reviewDetail && (reviewDetail.people_entries || reviewDetail.people_names)) || [];
  const set = {{}};
  entries.forEach(function(e) {{
    const slug = (typeof e === 'string') ? e : (e.slug || '');
    if (slug) set[slug.toLowerCase()] = slug;
  }});
  return set;
}}

function buildPersonPickerEntries(face) {{
  const folderSet = folderSlugSet();
  const entries = (reviewDetail && (reviewDetail.people_entries || reviewDetail.people_names)) || [];
  const out = [];
  const seen = {{}};
  entries.forEach(function(e) {{
    const slug = (typeof e === 'string') ? e : (e.slug || '');
    if (!slug || seen[slug.toLowerCase()]) return;
    seen[slug.toLowerCase()] = true;
    const label = (typeof e === 'string') ? personLabel(e) : (e.display_name || e.slug || e);
    out.push({{ slug: slug, display_name: label, in_folder: true }});
  }});
  const detected = (face && face.person_name) ? String(face.person_name).trim() : '';
  if (detected && !seen[detected.toLowerCase()]) {{
    seen[detected.toLowerCase()] = true;
    out.push({{
      slug: detected,
      display_name: personLabel(detected),
      in_folder: !!folderSet[detected.toLowerCase()],
    }});
  }}
  out.sort(function(a, b) {{
    return String(a.display_name).localeCompare(String(b.display_name), undefined, {{ sensitivity: 'base' }});
  }});
  return out;
}}

function getFacePick(faceId) {{
  if (Object.prototype.hasOwnProperty.call(reviewFacePicks, faceId)) {{
    return reviewFacePicks[faceId];
  }}
  const face = ((reviewDetail && reviewDetail.faces) || []).find(function(f) {{ return f.face_id === faceId; }});
  return face && face.person_name ? String(face.person_name) : '';
}}

function setFacePick(faceId, slug) {{
  reviewFacePicks[faceId] = slug || '';
}}

function reviewAssignmentsDirty() {{
  if (!reviewDetail) return false;
  const faces = reviewDetail.faces || [];
  for (let i = 0; i < faces.length; i++) {{
    const id = faces[i].face_id;
    const cur = getFacePick(id) || '';
    const init = Object.prototype.hasOwnProperty.call(reviewFacePicksInitial, id)
      ? (reviewFacePicksInitial[id] || '')
      : (faces[i].person_name || '');
    if (cur !== init) return true;
  }}
  return false;
}}

function closeAllPersonPickers() {{
  if (reviewOpenPickerFaceId == null) return;
  reviewOpenPickerFaceId = null;
  if (reviewDetail) renderFacePanel();
}}

function togglePersonPicker(faceId, ev) {{
  if (ev) ev.stopPropagation();
  reviewOpenPickerFaceId = (reviewOpenPickerFaceId === faceId) ? null : faceId;
  renderFacePanel();
  if (reviewOpenPickerFaceId === faceId) {{
    const search = document.getElementById('review_picker_search_' + faceId);
    if (search) {{
      search.value = '';
      filterPersonPicker(faceId);
      search.focus();
    }}
  }}
}}

function filterPersonPicker(faceId) {{
  const search = document.getElementById('review_picker_search_' + faceId);
  const list = document.getElementById('review_picker_list_' + faceId);
  if (!list) return;
  const q = ((search && search.value) || '').trim().toLowerCase();
  list.querySelectorAll('.person-picker-item[data-slug]').forEach(function(btn) {{
    const slug = (btn.dataset.slug || '').toLowerCase();
    const label = (btn.dataset.label || '').toLowerCase();
    const show = !q || slug.indexOf(q) >= 0 || label.indexOf(q) >= 0;
    btn.style.display = show ? '' : 'none';
  }});
}}

function pickReviewPerson(faceId, slug, ev) {{
  if (ev) ev.stopPropagation();
  setFacePick(faceId, slug || '');
  reviewOpenPickerFaceId = null;
  renderFacePanel();
  renderFaceBoxes();
  // Persist immediately so named faces get cropped into the person folder
  // (not only when closing or clicking Add faces / Move to blocked).
  persistFaceAssignment(faceId, slug || '');
}}

async function persistFaceAssignment(faceId, slug) {{
  if (!reviewDetail || !reviewAssetId || !reviewFolder) return;
  try {{
    const body = new URLSearchParams();
    body.set('asset_id', String(reviewAssetId));
    body.set('folder', reviewFolder);
    body.set('status', reviewStatus);
    body.set('faces', JSON.stringify([{{ face_id: faceId, person_name: slug || '' }}]));
    const r = await fetch('/api/review_photo/save_assignments', {{ method: 'POST', body: body }});
    const d = await r.json();
    if (!d.ok) {{
      alert(d.error || t('common.request_failed'));
      return;
    }}
    reviewFacePicksInitial[faceId] = slug || '';
    const face = (reviewDetail.faces || []).find(function(f) {{ return f.face_id === faceId; }});
    if (face) face.person_name = slug || null;
    if (d.reprocessed && d.new_status && d.new_status !== reviewStatus) {{
      closeReviewModal();
      loadReviewPhotos();
      return;
    }}
    if (slug && d.crops_written === 0 && reviewDetail.missing_on_disk) {{
      // Source missing — assignment saved in DB only.
    }}
  }} catch (e) {{
    alert(t('common.request_failed'));
  }}
}}

function personPickerHtml(face) {{
  const faceId = face.face_id;
  const selected = getFacePick(faceId);
  const entries = buildPersonPickerEntries(face);
  const toggleLabel = selected
    ? personLabel(selected)
    : t('review.person.unknown_option');
  const isOpen = reviewOpenPickerFaceId === faceId;
  let panelHtml = '';
  if (isOpen) {{
    const items = [
      '<button type="button" class="person-picker-item" data-slug="" data-label="' +
        escapeHtmlAttr(t('review.person.unknown_option')) +
        '" onclick="pickReviewPerson(' + faceId + ', this.dataset.slug, event)">' +
        escapeHtml(t('review.person.unknown_option')) + '</button>'
    ];
    entries.forEach(function(e) {{
      const hint = e.in_folder ? '' : ('<span class="hint">' + escapeHtml(t('review.person.not_in_folder')) + '</span>');
      const active = (e.slug === selected) ? ' active' : '';
      items.push(
        '<button type="button" class="person-picker-item' + active + '" data-slug="' + escapeHtmlAttr(e.slug) +
        '" data-label="' + escapeHtmlAttr(e.display_name) +
        '" onclick="pickReviewPerson(' + faceId + ', this.dataset.slug, event)">' +
        escapeHtml(e.display_name) + hint + '</button>'
      );
    }});
    panelHtml =
      '<div class="person-picker-panel" id="review_picker_panel_' + faceId + '">' +
        '<input type="search" class="person-picker-search" id="review_picker_search_' + faceId +
          '" placeholder="' + escapeHtmlAttr(t('review.person.search_placeholder')) +
          '" oninput="filterPersonPicker(' + faceId + ')" onclick="event.stopPropagation();" ' +
          'onkeydown="personPickerKeydown(' + faceId + ', event)">' +
        '<div class="person-picker-list" id="review_picker_list_' + faceId + '">' + items.join('') + '</div>' +
        '<div class="person-picker-footer">' +
          '<button type="button" onclick="openReviewAddPersonModal(' + faceId + ', event)">' +
            escapeHtml(t('review.person.add_new')) + '</button>' +
        '</div>' +
      '</div>';
  }}
  return (
    '<div class="person-picker" id="review_person_picker_' + faceId + '" onclick="event.stopPropagation();">' +
      '<button type="button" class="person-picker-toggle" id="review_person_' + faceId + '" data-value="' +
        escapeHtmlAttr(selected) + '" onclick="togglePersonPicker(' + faceId + ', event)">' +
        escapeHtml(toggleLabel) + '</button>' +
      panelHtml +
    '</div>'
  );
}}

function personPickerKeydown(faceId, ev) {{
  if (ev.key === 'Escape') {{
    ev.preventDefault();
    reviewOpenPickerFaceId = null;
    renderFacePanel();
    return;
  }}
  if (ev.key !== 'Enter') return;
  ev.preventDefault();
  const list = document.getElementById('review_picker_list_' + faceId);
  if (!list) return;
  const visible = Array.prototype.filter.call(
    list.querySelectorAll('.person-picker-item[data-slug]'),
    function(btn) {{ return btn.style.display !== 'none'; }}
  );
  if (!visible.length) return;
  const slug = visible[0].dataset.slug || '';
  pickReviewPerson(faceId, slug, ev);
}}

function renderFaceBoxes() {{
  const layer = document.getElementById('review_boxes');
  const img = document.getElementById('review_preview');
  if (!layer || !img || !reviewDetail) return;
  layer.innerHTML = '';
  const pw = reviewDetail.preview_w || img.naturalWidth || 1;
  const ph = reviewDetail.preview_h || img.naturalHeight || 1;
  (reviewDetail.faces || []).forEach(function(f) {{
    const b = f.bbox || [];
    if (b.length !== 4) return;
    const x1 = b[0], y1 = b[1], x2 = b[2], y2 = b[3];
    const left = (100 * x1 / pw).toFixed(3);
    const top = (100 * y1 / ph).toFixed(3);
    const w = (100 * (x2 - x1) / pw).toFixed(3);
    const h = (100 * (y2 - y1) / ph).toFixed(3);
    const pick = getFacePick(f.face_id);
    const label = pick ? personLabel(pick) : t('review.face.unknown');
    const sel = (reviewSelectedFaceId === f.face_id) ? ' selected' : '';
    const box = document.createElement('div');
    box.className = 'face-box' + sel;
    box.style.left = left + '%';
    box.style.top = top + '%';
    box.style.width = w + '%';
    box.style.height = h + '%';
    box.dataset.faceId = String(f.face_id);
    box.innerHTML = '<span class="face-box-label">' + escapeHtml(label) + '</span>';
    box.onclick = function(ev) {{ ev.stopPropagation(); selectReviewFace(f.face_id); }};
    layer.appendChild(box);
  }});
}}

function renderFacePanel() {{
  const panel = document.getElementById('review_faces_panel');
  if (!panel || !reviewDetail) return;
  const folderSet = folderSlugSet();
  panel.innerHTML = (reviewDetail.faces || []).map(function(f) {{
    const sel = (reviewSelectedFaceId === f.face_id) ? ' selected' : '';
    const score = (f.match_score != null) ? (' ' + t('review.face.score', {{n: f.match_score.toFixed(1)}})) : '';
    const detected = f.person_name
      ? t('review.face.detected', {{name: escapeHtml(personLabel(f.person_name))}})
      : t('review.face.unknown_face');
    const pick = getFacePick(f.face_id);
    const missingFolder = !!(pick && !folderSet[pick.toLowerCase()]);
    const ensureBtn = missingFolder
      ? ('<button type="button" class="btn-add-folder" onclick="ensureReviewPersonFolder(' +
         f.face_id + ', event)">' + escapeHtml(t('review.person.add_to_folder')) + '</button>')
      : '';
    return '<div class="review-face-row' + sel + '" data-face-id="' + f.face_id + '" onclick="selectReviewFace(' + f.face_id + ')">' +
      '<div><strong>' + escapeHtml(t('review.face.heading', {{id: f.face_id}})) + '</strong> — ' + detected + score + '</div>' +
      '<div class="review-face-actions" onclick="event.stopPropagation();">' +
        personPickerHtml(f) + ensureBtn + '</div></div>';
  }}).join('');
}}

function selectReviewFace(faceId) {{
  reviewSelectedFaceId = faceId;
  renderFaceBoxes();
  renderFacePanel();
}}

function mergePeopleEntry(slug, displayName) {{
  if (!reviewDetail) return;
  if (!reviewDetail.people_entries) reviewDetail.people_entries = [];
  if (!reviewDetail.people_labels) reviewDetail.people_labels = {{}};
  reviewDetail.people_labels[slug] = displayName || slug;
  const exists = reviewDetail.people_entries.some(function(e) {{
    return ((typeof e === 'string') ? e : e.slug) === slug;
  }});
  if (!exists) {{
    reviewDetail.people_entries.push({{ slug: slug, display_name: displayName || slug }});
  }}
  (reviewDetail.faces || []).forEach(function(f) {{
    if (f.person_name === slug) f.in_people_folder = true;
  }});
}}

async function ensureReviewPersonFolder(faceId, ev) {{
  if (ev) ev.stopPropagation();
  const slug = getFacePick(faceId);
  if (!slug) return;
  try {{
    const body = new URLSearchParams();
    body.set('name', slug);
    body.set('display_name', personLabel(slug));
    const r = await fetch('/api/people/ensure_folder', {{ method: 'POST', body: body }});
    const d = await r.json();
    if (!d.ok) {{
      alert(d.error || t('review.person.ensure_failed'));
      return;
    }}
    mergePeopleEntry(d.slug || slug, d.display_name || personLabel(slug));
    setFacePick(faceId, d.slug || slug);
    renderFacePanel();
    renderFaceBoxes();
  }} catch (e) {{
    alert(t('common.request_failed'));
  }}
}}

function openReviewAddPersonModal(faceId, ev) {{
  if (ev) ev.stopPropagation();
  closeAllPersonPickers();
  reviewAddPersonFaceId = faceId;
  const modal = document.getElementById('review_add_person_modal');
  const first = document.getElementById('review_add_first');
  const last = document.getElementById('review_add_last');
  const consent = document.getElementById('review_add_consent');
  if (first) first.value = '';
  if (last) last.value = '';
  if (consent) consent.value = 'blocked';
  if (modal) modal.classList.add('open');
  if (first) first.focus();
}}

function closeReviewAddPersonModal() {{
  const modal = document.getElementById('review_add_person_modal');
  if (modal) modal.classList.remove('open');
  reviewAddPersonFaceId = null;
}}

async function submitReviewAddPerson(ev) {{
  ev.preventDefault();
  const first = ((document.getElementById('review_add_first') || {{}}).value || '').trim();
  const last = ((document.getElementById('review_add_last') || {{}}).value || '').trim();
  const consent = ((document.getElementById('review_add_consent') || {{}}).value || 'blocked').trim();
  if (!first || !last) return false;
  const faceId = reviewAddPersonFaceId;
  const fd = new FormData();
  fd.set('first_name', first);
  fd.set('last_name', last);
  fd.set('consent', consent);
  try {{
    const r = await fetch('/api/people/create', {{ method: 'POST', body: fd }});
    const d = await r.json();
    if (!d.ok) {{
      alert(d.error || t('review.person.create_failed'));
      return false;
    }}
    mergePeopleEntry(d.slug, d.display_name || d.slug);
    if (faceId != null) {{
      setFacePick(faceId, d.slug);
      reviewOpenPickerFaceId = null;
      closeReviewAddPersonModal();
      renderFacePanel();
      renderFaceBoxes();
      await persistFaceAssignment(faceId, d.slug);
    }} else {{
      closeReviewAddPersonModal();
    }}
  }} catch (e) {{
    alert(t('common.request_failed'));
  }}
  return false;
}}

document.addEventListener('click', function(ev) {{
  if (ev.target.closest('.person-picker')) return;
  closeAllPersonPickers();
}});

async function openReviewAtIndex(idx) {{
  if (idx < 0 || idx >= reviewPhotos.length) return;
  if (reviewAssetId && reviewAssignmentsDirty()) {{
    const saved = await saveCurrentReviewAssignments();
    if (!saved) return;
  }}
  reviewPhotoIndex = idx;
  await openReviewModal(reviewPhotos[idx].asset_id);
  updateReviewNavUi();
}}

function reviewNav(delta) {{
  openReviewAtIndex(reviewPhotoIndex + delta);
}}

async function openReviewModal(assetId) {{
  reviewAssetId = assetId;
  reviewSelectedFaceId = null;
  reviewFacePicks = {{}};
  reviewFacePicksInitial = {{}};
  reviewOpenPickerFaceId = null;
  closeReviewAddPersonModal();
  const modal = document.getElementById('review_modal');
  const title = document.getElementById('review_modal_title');
  const meta = document.getElementById('review_modal_meta');
  const img = document.getElementById('review_preview');
  const toBlocked = document.getElementById('review_to_blocked_btn');
  const toOk = document.getElementById('review_to_ok_btn');
  if (!modal || !reviewFolder) return;
  if (meta) meta.textContent = t('review.meta.loading');
  modal.classList.add('open');
  updateReviewNavUi();
  try {{
    const r = await fetch('/api/review_photo?id=' + assetId + '&folder=' + encodeURIComponent(reviewFolder) + '&status=' + encodeURIComponent(reviewStatus));
    const d = await r.json();
    if (!d.ok) {{
      if (meta) meta.textContent = d.error || t('review.meta.photo_failed');
      return;
    }}
    reviewDetail = d;
    (d.faces || []).forEach(function(f) {{
      const name = f.person_name ? String(f.person_name) : '';
      reviewFacePicks[f.face_id] = name;
      reviewFacePicksInitial[f.face_id] = name;
    }});
    const kind = reviewKindLabel();
    if (title) title.textContent = d.name || t('review.modal.title_fallback', {{kind: kind}});
    if (meta) meta.textContent = (d.reason || '') + (d.missing_on_disk ? (' ' + t('review.meta.missing_disk')) : '');
    const disabled = !!d.missing_on_disk;
    if (toBlocked) toBlocked.disabled = disabled;
    if (toOk) toOk.disabled = disabled;
    if (img) {{
      img.onload = function() {{ renderFaceBoxes(); }};
      img.src = d.preview_url || '';
    }}
    if ((d.faces || []).length) reviewSelectedFaceId = d.faces[0].face_id;
    renderFacePanel();
    renderFaceBoxes();
    updateReviewNavUi();
  }} catch (e) {{
    if (meta) meta.textContent = t('common.request_failed');
  }}
}}

function collectDirtyFaceAssignments() {{
  const out = [];
  if (!reviewDetail) return out;
  (reviewDetail.faces || []).forEach(function(f) {{
    const cur = (getFacePick(f.face_id) || '').trim();
    const init = Object.prototype.hasOwnProperty.call(reviewFacePicksInitial, f.face_id)
      ? String(reviewFacePicksInitial[f.face_id] || '').trim()
      : String(f.person_name || '').trim();
    if (cur !== init) {{
      out.push({{ face_id: f.face_id, person_name: cur }});
    }}
  }});
  return out;
}}

async function saveCurrentReviewAssignments() {{
  if (!reviewDetail || !reviewAssetId || !reviewFolder) return true;
  const assignments = collectDirtyFaceAssignments();
  if (!assignments.length) return true;
  try {{
    const body = new URLSearchParams();
    body.set('asset_id', String(reviewAssetId));
    body.set('folder', reviewFolder);
    body.set('status', reviewStatus);
    body.set('faces', JSON.stringify(assignments));
    const r = await fetch('/api/review_photo/save_assignments', {{ method: 'POST', body: body }});
    const d = await r.json();
    if (!d.ok) {{
      alert(d.error || t('common.request_failed'));
      return false;
    }}
    assignments.forEach(function(a) {{
      reviewFacePicksInitial[a.face_id] = a.person_name || '';
      const face = (reviewDetail.faces || []).find(function(f) {{ return f.face_id === a.face_id; }});
      if (face) face.person_name = a.person_name || null;
    }});
    return true;
  }} catch (e) {{
    alert(t('common.request_failed'));
    return false;
  }}
}}

async function closeReviewModal() {{
  if (reviewAssetId && reviewAssignmentsDirty()) {{
    const saved = await saveCurrentReviewAssignments();
    if (!saved) return;
  }}
  reviewOpenPickerFaceId = null;
  const modal = document.getElementById('review_modal');
  if (modal) modal.classList.remove('open');
  reviewDetail = null;
  reviewAssetId = 0;
  reviewFacePicks = {{}};
  reviewFacePicksInitial = {{}};
  reviewPhotoIndex = -1;
  highlightReviewThumb();
}}

function collectFaceAssignments() {{
  const out = [];
  if (!reviewDetail) return out;
  (reviewDetail.faces || []).forEach(function(f) {{
    const person = (getFacePick(f.face_id) || '').trim();
    if (person) out.push({{ face_id: f.face_id, person_name: person }});
  }});
  return out;
}}

async function confirmReviewOk() {{
  if (!reviewDetail || !reviewAssetId || !reviewFolder) return;
  const msg = reviewStatus === 'blocked'
    ? t('review.confirm.ok_from_blocked')
    : t('review.confirm.ok_from_review');
  if (!confirm(msg)) return;
  if (reviewAssignmentsDirty()) {{
    const saved = await saveCurrentReviewAssignments();
    if (!saved) return;
  }}
  const meta = document.getElementById('review_modal_meta');
  if (meta) meta.textContent = t('review.meta.processing');
  try {{
    const body = new URLSearchParams();
    body.set('asset_id', String(reviewAssetId));
    body.set('folder', reviewFolder);
    body.set('from_status', reviewStatus);
    const r = await fetch('/api/review_photo/confirm_ok', {{ method: 'POST', body: body }});
    const d = await r.json();
    if (!d.ok) {{
      alert(d.error || t('review.alert.move_ok_failed'));
      if (meta) meta.textContent = d.error || t('common.failed');
      return;
    }}
    await afterReviewProcessed(d.message || t('common.done'));
  }} catch (e) {{
    alert(t('common.request_failed'));
  }}
}}

async function afterReviewProcessed(message) {{
  // Status-changing actions already persisted assignments; avoid re-save as review.
  reviewFacePicksInitial = Object.assign({{}}, reviewFacePicks);
  const keepIdx = reviewPhotoIndex;
  const modal = document.getElementById('review_modal');
  const stayOpen = modal && modal.classList.contains('open');
  await loadReviewList(stayOpen);
  if (stayOpen && reviewPhotos.length) {{
    await openReviewAtIndex(Math.min(keepIdx, reviewPhotos.length - 1));
  }} else {{
    await closeReviewModal();
  }}
  if (message) {{
    const meta = document.getElementById('review_modal_meta');
    if (meta && stayOpen && reviewPhotos.length) meta.textContent = message;
    else if (message) alert(message);
  }}
}}

async function confirmReviewBlocked() {{
  if (!reviewDetail || !reviewAssetId || !reviewFolder) return;
  const assignments = collectFaceAssignments();
  if (!assignments.length) {{
    alert(t('review.alert.assign_required'));
    return;
  }}
  const summary = assignments.map(function(a) {{ return a.person_name + ' (face ' + a.face_id + ')'; }}).join('\\n');
  const confirmKey = reviewStatus === 'blocked' ? 'review.confirm.add_faces' : 'review.confirm.blocked';
  if (!confirm(t(confirmKey, {{summary: summary}}))) return;
  const meta = document.getElementById('review_modal_meta');
  if (meta) meta.textContent = t('review.meta.processing');
  try {{
    const body = new URLSearchParams();
    body.set('asset_id', String(reviewAssetId));
    body.set('folder', reviewFolder);
    body.set('status', reviewStatus);
    body.set('faces', JSON.stringify(assignments));
    const r = await fetch('/api/review_photo/confirm_blocked', {{ method: 'POST', body: body }});
    const d = await r.json();
    if (!d.ok) {{
      alert(d.error || t('review.alert.confirm_blocked_failed'));
      if (meta) meta.textContent = d.error || t('common.failed');
      return;
    }}
    await afterReviewProcessed(d.message || t('common.done'));
  }} catch (e) {{
    alert(t('common.request_failed'));
  }}
}}

async function batchReviewBlocked() {{
  if (reviewStatus !== 'review' || !reviewFolder) return;
  const n = reviewPhotos.length;
  if (!n) return;
  if (!confirm(t('review.confirm.batch', {{n: n}}))) return;
  await closeReviewModal();
  const meta = document.getElementById('review_list_meta');
  if (meta) meta.textContent = t('review.meta.batch_processing');
  try {{
    const body = new URLSearchParams();
    body.set('folder', reviewFolder);
    const r = await fetch('/api/review_photo/batch_blocked', {{ method: 'POST', body: body }});
    const d = await r.json();
    if (!d.ok) {{
      alert(d.error || t('review.alert.batch_failed'));
      if (meta) meta.textContent = d.error || t('review.meta.batch_failed');
      return;
    }}
    await loadReviewList(false);
    let msg = d.message || t('common.done');
    if (d.skipped_items && d.skipped_items.length) {{
      msg += '\\n\\n' + t('review.alert.skipped_prefix') + '\\n' + d.skipped_items.join('\\n');
    }}
    if (d.error_items && d.error_items.length) {{
      msg += '\\n\\n' + t('review.alert.errors_prefix') + '\\n' + d.error_items.join('\\n');
    }}
    alert(msg);
  }} catch (e) {{
    alert(t('common.request_failed'));
    if (meta) meta.textContent = t('common.request_failed');
  }}
}}

document.addEventListener('keydown', function(ev) {{
  const modal = document.getElementById('review_modal');
  const open = modal && modal.classList.contains('open');
  if (ev.key === 'Escape') {{
    if (reviewOpenPickerFaceId != null) {{
      reviewOpenPickerFaceId = null;
      if (reviewDetail) renderFacePanel();
      return;
    }}
    const addModal = document.getElementById('review_add_person_modal');
    if (addModal && addModal.classList.contains('open')) {{
      closeReviewAddPersonModal();
      return;
    }}
    closeReviewModal();
    return;
  }}
  if (!open) return;
  if (ev.key === 'ArrowLeft') {{ ev.preventDefault(); reviewNav(-1); }}
  if (ev.key === 'ArrowRight') {{ ev.preventDefault(); reviewNav(1); }}
}});

(function autoLoad() {{
  const folder = (document.getElementById('review_folder') || {{}}).value || '';
  if (folder.trim()) loadReviewList();
}})();
</script>
</body></html>"""
            self._send_html(body)
            return
        if parsed.path == "/people":
            lang = self._lang()
            people_root = (STATE.people_root_last or _load_people_dir_from_config()).strip()
            people_rows = _list_people_rows()
            people_mismatch = _people_mismatch_warn_visible(people_rows)
            table_body = _people_table_body_html(people_rows, lang=lang)
            all_tags_js = json.dumps(_collect_all_people_tags(people_rows))
            body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(_t("app.title.people", lang))}</title>
{self._base_style()}
</head><body>
{self._header("people", "subtitle.people", people_mismatch=people_mismatch)}
<fieldset><legend>{html.escape(_t("people.folder.legend", lang))}</legend>
  <form method="post" action="/scan_people" onsubmit="return scanPeople(event)">
    <div class="row">
      <input id="people_root_people" type="text" name="people_root" placeholder="{html.escape(_t("people.folder.placeholder", lang))}" value="{html.escape(people_root)}">
      <button type="button" onclick="pickFolder('people_root_people')">{html.escape(_t("common.choose_finder", lang))}</button>
    </div>
    <div class="row" style="color:var(--muted);font-size:13px">
      {html.escape(_t("people.scan.hint", lang))}
    </div>
    <button type="submit">{html.escape(_t("people.btn.scan", lang))}</button>
    <div id="scan_result" style="margin-top:10px;color:var(--muted);font-size:13px;"></div>
    <div id="scan_progress" style="margin-top:6px;color:var(--muted);font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"></div>
  </form>
</fieldset>

<fieldset>
  <div class="people-list-toolbar">
    <span class="people-list-title">{html.escape(_t("people.list.title", lang))}</span>
    <button type="button" onclick="openAddPerson()">{html.escape(_t("people.btn.add", lang))}</button>
    <input type="search" id="people_search" placeholder="{html.escape(_t("people.search.placeholder", lang))}" oninput="filterPeopleTable()">
  </div>
  <div id="people_search_empty" style="display:none;color:var(--muted);font-size:13px;margin-bottom:8px;">{html.escape(_t("people.search.empty", lang))}</div>
  <table class="people-table" style="width:100%;border-collapse:collapse">
  <thead><tr>
    {_people_name_column_header_html(lang)}
    <th class="sortable" style="text-align:left" data-sort-key="photos" onclick="sortPeopleTable('photos')">{html.escape(_t("people.col.photos", lang))}</th>
    <th class="sortable" style="text-align:left" data-sort-key="faces" onclick="sortPeopleTable('faces')">{html.escape(_t("people.col.faces", lang))}{_help_mark(_t("people.col.faces_help", lang))}</th>
    <th class="sortable" style="text-align:left" data-sort-key="consent" onclick="sortPeopleTable('consent')">{html.escape(_t("people.col.consent", lang))}</th>
    <th class="sortable" style="text-align:left" data-sort-key="tags" onclick="sortPeopleTable('tags')">{html.escape(_t("people.col.tags", lang))}</th>
    <th style="text-align:right"><button type="button" class="btn-small btn-muted" onclick="reregisterAllPeople()">{html.escape(_t("people.btn.register_all", lang))}</button></th>
  </tr></thead>
  <tbody id="people_table_body">{table_body}</tbody>
  </table>
</fieldset>

<div id="tag_picker" class="tag-picker" style="display:none;"></div>

<div id="person_modal" class="modal-backdrop" onclick="if(event.target===this) closePersonModal()">
  <div class="modal" style="max-width:520px;">
    <button type="button" class="close-btn" onclick="closePersonModal()">{html.escape(_t("common.close", lang))}</button>
    <h2 id="person_modal_title">{html.escape(_t("people.modal.add_title", lang))}</h2>
    <form id="person_form" onsubmit="return submitPersonForm(event)">
      <input type="hidden" id="person_edit_slug" value="">
      <div class="person-form-grid">
        <div><label for="person_first_name">{html.escape(_t("people.label.vorname", lang))}</label><input id="person_first_name" required oninput="updatePersonSlugPreview()"></div>
        <div><label for="person_last_name">{html.escape(_t("people.label.nachname", lang))}</label><input id="person_last_name" required oninput="updatePersonSlugPreview()"></div>
        <div><label for="person_display_name">{html.escape(_t("people.label.display_name", lang))}</label><input id="person_display_name" placeholder="{html.escape(_t("people.placeholder.display_name", lang))}"></div>
        <div id="person_slug_row"><span class="person-slug-preview">{html.escape(_t("people.slug.prefix", lang))} <span id="person_slug_preview">{html.escape(_t("people.dash", lang))}</span></span></div>
        <div><label for="person_consent">{html.escape(_t("people.label.consent", lang))}</label>
          <select id="person_consent"><option value="blocked" selected>{html.escape(_t("people.consent.blocked", lang))}</option><option value="allowed">{html.escape(_t("people.consent.allowed", lang))}</option></select>
        </div>
        <div><label for="person_photos">{html.escape(_t("people.label.photos", lang))}</label><input id="person_photos" type="file" accept="image/*" multiple></div>
      </div>
      <div style="margin-top:16px;display:flex;gap:8px;">
        <button type="submit" id="person_submit_btn">{html.escape(_t("people.btn.create", lang))}</button>
      </div>
    </form>
  </div>
</div>

<div id="gallery_modal" class="modal-backdrop" onclick="if(event.target===this) closeGallery()">
  <div class="modal">
    <button type="button" class="close-btn" onclick="closeGallery()">{html.escape(_t("common.close", lang))}</button>
    <h2 id="gallery_title">{html.escape(_t("people.gallery.title", lang))}</h2>
    <div id="gallery_meta" style="color:var(--muted);font-size:13px;"></div>
    <div id="gallery_hero" class="gallery-hero"></div>
    <div id="gallery_source" class="gallery-source"></div>
    <div id="gallery_match" class="gallery-match"></div>
    <div class="gallery-pos-row">
      <span id="gallery_pos_label" class="review-pos"></span>
    </div>
    <div style="margin:8px 0;text-align:center;">
      <button type="button" id="gallery_delete_btn" onclick="deleteGalleryPhoto()" style="background:#7b2432;border-radius:9px;display:none;">{html.escape(_t("people.gallery.delete", lang))}</button>
    </div>
    <div id="gallery_grid" class="gallery-grid"></div>
  </div>
</div>

<script>
function escapeHtml(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}
function escapeHtmlAttr(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
}}
let peopleAllTags = {all_tags_js};
let peopleSortKey = 'name';
let peopleSortDir = 'asc';
let peopleNameSortPrimary = 'first';
let tagPopoverSlug = '';
let tagPopoverPersonTags = [];

function closePeopleMenus() {{
  document.querySelectorAll('.people-menu-panel').forEach(function(p) {{ p.hidden = true; }});
}}

function togglePeopleRowMenu(ev, btn) {{
  ev.stopPropagation();
  const panel = btn && btn.nextElementSibling;
  const wasOpen = panel && !panel.hidden;
  closePeopleMenus();
  if (panel && !wasOpen) panel.hidden = false;
}}

document.addEventListener('click', function(ev) {{
  if (ev.target.closest('.people-row-menu')) return;
  if (ev.target.closest('#tag_picker')) return;
  if (ev.target.closest('.tag-add')) return;
  closePeopleMenus();
  closeTagPicker();
}});

(function initTagPickerDelegation() {{
  const pop = document.getElementById('tag_picker');
  if (!pop) return;
  pop.addEventListener('click', function(ev) {{
    const pick = ev.target.closest('.tag-picker-pick');
    if (pick) {{
      ev.stopPropagation();
      pickExistingTag(pick.getAttribute('data-tag') || '');
      return;
    }}
    const newBtn = ev.target.closest('.tag-picker-new-btn');
    if (newBtn) {{
      ev.stopPropagation();
      showNewTagInput(ev);
      return;
    }}
    const confirmBtn = ev.target.closest('.tag-picker-confirm');
    if (confirmBtn) {{
      ev.stopPropagation();
      confirmNewTagInput();
    }}
  }});
}})();

function filterPeopleTable() {{
  const q = ((document.getElementById('people_search') || {{}}).value || '').trim().toLowerCase();
  const tbody = document.getElementById('people_table_body');
  const empty = document.getElementById('people_search_empty');
  if (!tbody) return;
  let visible = 0;
  tbody.querySelectorAll('tr[data-search-text]').forEach(function(tr) {{
    const core = tr.dataset.searchCore || '';
    const tags = tr.dataset.searchTags || '';
    const hay = ((core + ' ' + tags).trim() || (tr.dataset.searchText || '')).toLowerCase();
    const show = !q || hay.indexOf(q) >= 0;
    tr.style.display = show ? '' : 'none';
    if (show) visible += 1;
  }});
  if (empty) empty.style.display = (q && visible === 0) ? 'block' : 'none';
}}

function comparePeopleNameKeys(a, b) {{
  const av = foldGermanUmlauts(String(a || '')).toLowerCase();
  const bv = foldGermanUmlauts(String(b || '')).toLowerCase();
  if (av < bv) return -1;
  if (av > bv) return 1;
  return 0;
}}

function updatePeopleNameHeader() {{
  const th = document.querySelector('.people-table th.people-name-col');
  if (!th) return;
  th.classList.remove('sort-asc', 'sort-desc');
  if (peopleSortKey === 'name') {{
    th.classList.add(peopleSortDir === 'asc' ? 'sort-asc' : 'sort-desc');
  }}
  th.querySelectorAll('.people-name-sort-part').forEach(function(btn) {{
    const active = peopleSortKey === 'name' && btn.dataset.namePart === peopleNameSortPrimary;
    btn.classList.toggle('sort-active', active);
  }});
}}

function formatPersonDisplayName(tr) {{
  const first = tr.dataset.displayFirst || '';
  const last = tr.dataset.displayLast || '';
  const fallback = tr.dataset.displayFallback || '';
  if (peopleNameSortPrimary === 'last' && first && last) {{
    return last + ', ' + first;
  }}
  if (first && last) return first + ' ' + last;
  return fallback;
}}

function updatePeopleNameDisplay() {{
  document.querySelectorAll('#people_table_body tr[data-display-first]').forEach(function(tr) {{
    const label = tr.querySelector('.person-link-label');
    if (label) label.textContent = formatPersonDisplayName(tr);
  }});
}}

function applyPeopleTableSort() {{
  const tbody = document.getElementById('people_table_body');
  if (!tbody) return;
  const key = peopleSortKey;
  const rows = Array.from(tbody.querySelectorAll('tr[data-search-text]'));
  rows.sort(function(a, b) {{
    if (key === 'name') {{
      const primary = peopleNameSortPrimary === 'last' ? 'sortLast' : 'sortFirst';
      const secondary = peopleNameSortPrimary === 'last' ? 'sortFirst' : 'sortLast';
      let cmp = comparePeopleNameKeys(a.dataset[primary], b.dataset[primary]);
      if (cmp !== 0) return peopleSortDir === 'asc' ? cmp : -cmp;
      cmp = comparePeopleNameKeys(a.dataset[secondary], b.dataset[secondary]);
      return peopleSortDir === 'asc' ? cmp : -cmp;
    }}
    const attr = {{
      photos: 'sortPhotos',
      faces: 'sortFaces',
      consent: 'sortConsent',
      tags: 'sortTags',
    }}[key] || 'sortFirst';
    let av = a.dataset[attr] || '';
    let bv = b.dataset[attr] || '';
    if (key === 'photos' || key === 'faces' || key === 'consent') {{
      av = parseFloat(av) || 0;
      bv = parseFloat(bv) || 0;
      return peopleSortDir === 'asc' ? av - bv : bv - av;
    }}
    av = String(av).toLowerCase();
    bv = String(bv).toLowerCase();
    if (av < bv) return peopleSortDir === 'asc' ? -1 : 1;
    if (av > bv) return peopleSortDir === 'asc' ? 1 : -1;
    return 0;
  }});
  rows.forEach(function(r) {{ tbody.appendChild(r); }});
  updatePeopleNameHeader();
  updatePeopleNameDisplay();
  filterPeopleTable();
  initTagsOverflow();
}}

function sortPeopleName(ev, part) {{
  if (ev) ev.stopPropagation();
  if (peopleSortKey === 'name' && peopleNameSortPrimary === part) {{
    peopleSortDir = (peopleSortDir === 'asc') ? 'desc' : 'asc';
  }} else {{
    peopleSortKey = 'name';
    peopleNameSortPrimary = part;
    peopleSortDir = 'asc';
  }}
  document.querySelectorAll('.people-table th.sortable:not(.people-name-col)').forEach(function(th) {{
    th.classList.remove('sort-asc', 'sort-desc');
  }});
  updatePeopleNameHeader();
  applyPeopleTableSort();
}}

function sortPeopleTable(key) {{
  if (key === 'name') {{
    sortPeopleName(null, peopleNameSortPrimary);
    return;
  }}
  if (peopleSortKey === key) {{
    peopleSortDir = (peopleSortDir === 'asc') ? 'desc' : 'asc';
  }} else {{
    peopleSortKey = key;
    peopleSortDir = 'asc';
  }}
  document.querySelectorAll('.people-table th.sortable').forEach(function(th) {{
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.sortKey === peopleSortKey) {{
      th.classList.add(peopleSortDir === 'asc' ? 'sort-asc' : 'sort-desc');
    }}
  }});
  updatePeopleNameHeader();
  applyPeopleTableSort();
}}

function foldGermanUmlauts(s) {{
  return String(s || '')
    .replace(/ä/g, 'ae').replace(/Ä/g, 'ae')
    .replace(/ö/g, 'oe').replace(/Ö/g, 'oe')
    .replace(/ü/g, 'ue').replace(/Ü/g, 'ue')
    .replace(/ß/g, 'ss');
}}

function slugPreviewFromNames(last, first) {{
  const clean = function(s) {{
    return foldGermanUmlauts(String(s || '').trim())
      .replace(/[<>:"/\\\\|?*\\x00-\\x1f]+/g, '')
      .toLowerCase();
  }};
  const l = clean(last);
  const f = clean(first).replace(/\\s+/g, '-');
  if (!l || !f) return t('people.dash');
  return l + '_' + f;
}}

function updatePersonSlugPreview() {{
  const last = (document.getElementById('person_last_name') || {{}}).value || '';
  const first = (document.getElementById('person_first_name') || {{}}).value || '';
  const el = document.getElementById('person_slug_preview');
  const row = document.getElementById('person_slug_row');
  const editSlug = (document.getElementById('person_edit_slug') || {{}}).value || '';
  if (el) el.textContent = editSlug || slugPreviewFromNames(last, first);
  if (row) row.style.display = editSlug ? 'none' : 'block';
}}

function openAddPerson() {{
  document.getElementById('person_modal_title').textContent = t('people.modal.add_title');
  document.getElementById('person_submit_btn').textContent = t('people.btn.create');
  document.getElementById('person_edit_slug').value = '';
  document.getElementById('person_first_name').value = '';
  document.getElementById('person_last_name').value = '';
  document.getElementById('person_display_name').value = '';
  document.getElementById('person_consent').value = 'blocked';
  document.getElementById('person_photos').value = '';
  updatePersonSlugPreview();
  document.getElementById('person_modal').classList.add('open');
}}

function openEditPerson(profile) {{
  if (!profile) return false;
  document.getElementById('person_modal_title').textContent = t('people.modal.edit_title');
  document.getElementById('person_submit_btn').textContent = t('people.btn.save');
  document.getElementById('person_edit_slug').value = profile.slug || '';
  document.getElementById('person_first_name').value = profile.first_name || '';
  document.getElementById('person_last_name').value = profile.last_name || '';
  document.getElementById('person_display_name').value = profile.display_name || '';
  document.getElementById('person_consent').value = profile.consent || 'blocked';
  document.getElementById('person_photos').value = '';
  updatePersonSlugPreview();
  document.getElementById('person_modal').classList.add('open');
  return false;
}}

function closePersonModal() {{
  document.getElementById('person_modal').classList.remove('open');
}}

async function submitPersonForm(ev) {{
  ev.preventDefault();
  const editSlug = (document.getElementById('person_edit_slug') || {{}}).value || '';
  const fd = new FormData();
  fd.set('first_name', (document.getElementById('person_first_name') || {{}}).value || '');
  fd.set('last_name', (document.getElementById('person_last_name') || {{}}).value || '');
  fd.set('display_name', (document.getElementById('person_display_name') || {{}}).value || '');
  fd.set('consent', (document.getElementById('person_consent') || {{}}).value || 'blocked');
  if (editSlug) fd.set('name', editSlug);
  const photos = document.getElementById('person_photos');
  if (photos && photos.files) {{
    for (let i = 0; i < photos.files.length; i++) fd.append('photos', photos.files[i]);
  }}
  const url = editSlug ? '/api/people/update' : '/api/people/create';
  const res = document.getElementById('scan_result');
  res.style.color = '#9aa4b2';
  res.textContent = editSlug ? t('people.msg.saving') : t('people.msg.creating');
  try {{
    const r = await fetch(url, {{ method: 'POST', body: fd }});
    const d = await r.json();
    if (!d.ok) {{
      res.style.color = '#ef6b73';
      res.textContent = d.error || t('common.failed');
      return false;
    }}
    res.style.color = '#3cc087';
    res.textContent = d.message || t('common.done');
    closePersonModal();
    await refreshPeopleTable();
  }} catch (e) {{
    res.style.color = '#ef6b73';
    res.textContent = t('people.msg.request_failed', {{e: e}});
  }}
  return false;
}}

function closeTagPicker() {{
  const pop = document.getElementById('tag_picker');
  if (pop) {{ pop.style.display = 'none'; pop.innerHTML = ''; }}
  tagPopoverSlug = '';
  tagPopoverPersonTags = [];
}}

function pickerTagsForPerson(personTags) {{
  const have = new Set((personTags || []).map(function(t) {{ return String(t).toLowerCase(); }}));
  return peopleAllTags.filter(function(t) {{ return !have.has(String(t).toLowerCase()); }});
}}

function renderTagPickerContent(available) {{
  const chips = available.map(function(t) {{
    return (
      '<button type="button" class="tag-picker-chip tag-picker-pick" data-tag="'
      + escapeHtmlAttr(t) + '">' + escapeHtml(t) + '</button>'
    );
  }}).join('');
  const emptyHint = available.length ? '' : '<span class="tag-picker-empty">' + escapeHtml(t('people.tag.picker_empty')) + '</span>';
  return (
    '<div class="tag-picker-row">'
    + emptyHint
    + chips
    + '<button type="button" class="tag-picker-chip tag-picker-new-btn">+</button>'
    + '</div>'
  );
}}

function repositionTagPicker() {{
  if (!tagPopoverSlug) return;
  let btn = null;
  document.querySelectorAll('.tag-add[data-person-slug]').forEach(function(el) {{
    if (el.getAttribute('data-person-slug') !== tagPopoverSlug) return;
    // Prefer the visible + (collapsed vs expanded rows both exist in the DOM).
    if (el.offsetParent === null && el.getClientRects().length === 0) return;
    btn = el;
  }});
  const pop = document.getElementById('tag_picker');
  if (!btn || !pop || pop.style.display === 'none') return;
  const rect = btn.getBoundingClientRect();
  const popRect = pop.getBoundingClientRect();
  const gap = 6;
  const margin = 8;
  let top = rect.bottom + gap;
  if (top + popRect.height > window.innerHeight - margin) {{
    top = rect.top - popRect.height - gap;
  }}
  if (top < margin) top = margin;
  let left = rect.left;
  if (left + popRect.width > window.innerWidth - margin) {{
    left = window.innerWidth - popRect.width - margin;
  }}
  if (left < margin) left = margin;
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
}}

function reopenTagPickerContent() {{
  const pop = document.getElementById('tag_picker');
  if (!pop || !tagPopoverSlug) return;
  const available = pickerTagsForPerson(tagPopoverPersonTags);
  pop.innerHTML = renderTagPickerContent(available);
  pop.style.display = 'block';
  requestAnimationFrame(function() {{ repositionTagPicker(); }});
}}

function openTagPicker(ev, slug, personTags) {{
  ev.stopPropagation();
  closePeopleMenus();
  tagPopoverSlug = slug;
  tagPopoverPersonTags = personTags || [];
  const pop = document.getElementById('tag_picker');
  if (!pop) return;
  const available = pickerTagsForPerson(tagPopoverPersonTags);
  pop.innerHTML = renderTagPickerContent(available);
  pop.style.display = 'block';
  // Measure after paint so we can flip above the button near the viewport bottom.
  requestAnimationFrame(function() {{ repositionTagPicker(); }});
}}

function initTagsOverflow() {{
  document.querySelectorAll('.tags-cell[data-tags-cell]').forEach(function(cell) {{
    const moreBtn = cell.querySelector('.tag-more');
    const total = parseInt(cell.dataset.total || '0', 10);
    const wasExpanded = cell.classList.contains('is-expanded');
    const hasOverflow = total > 8;
    cell.classList.toggle('has-overflow', hasOverflow);
    if (!hasOverflow) {{
      cell.classList.remove('is-expanded');
    }} else if (wasExpanded) {{
      cell.classList.add('is-expanded');
    }}
    const expanded = cell.classList.contains('is-expanded');
    if (moreBtn) {{
      moreBtn.textContent = expanded ? t('people.tag.less') : t('people.tag.more');
    }}
  }});
}}

function toggleTagsExpand(ev, btn) {{
  if (ev) ev.stopPropagation();
  const cell = btn && btn.closest('.tags-cell');
  if (!cell) return;
  const expanded = cell.classList.toggle('is-expanded');
  btn.textContent = expanded ? t('people.tag.less') : t('people.tag.more');
}}

function findPersonRow(slug) {{
  const key = String(slug || '').toLowerCase();
  if (!key) return null;
  const rows = document.querySelectorAll('#people_table_body tr[data-person-name]');
  for (let i = 0; i < rows.length; i++) {{
    if ((rows[i].dataset.personName || '') === key) return rows[i];
  }}
  return null;
}}

function applyPersonTagsUpdate(slug, d, opts) {{
  opts = opts || {{}};
  const tr = findPersonRow(slug);
  if (!tr || !d || !d.tags_html) {{
    return refreshPeopleTable(opts);
  }}
  const cell = tr.querySelector('.tags-cell');
  const wasExpanded = !!(cell && cell.classList.contains('is-expanded'));
  const td = cell ? cell.parentElement : null;
  if (!td) {{
    return refreshPeopleTable(opts);
  }}
  td.innerHTML = d.tags_html;
  const neu = td.querySelector('.tags-cell');
  if (neu) {{
    const hasOverflow = parseInt(neu.dataset.total || '0', 10) > 8;
    neu.classList.toggle('has-overflow', hasOverflow);
    if (wasExpanded && hasOverflow) {{
      neu.classList.add('is-expanded');
      const moreBtn = neu.querySelector('.tag-more');
      if (moreBtn) moreBtn.textContent = t('people.tag.less');
    }}
  }}
  if (d.sort_tags != null) tr.dataset.sortTags = d.sort_tags;
  if (d.tag_names) {{
    tr.dataset.searchTags = d.tag_names.join(' ').toLowerCase();
    const core = tr.dataset.searchCore || '';
    tr.dataset.searchText = (core + ' ' + tr.dataset.searchTags).trim();
  }}
  if (opts.keepPickerOpen && tagPopoverSlug === slug) {{
    tagPopoverPersonTags = tagNamesFromResponse(d);
    reopenTagPickerContent();
  }}
  return Promise.resolve();
}}

function tagNamesFromResponse(d) {{
  return (d.tags || []).map(function(t) {{ return t.tag || t; }}).filter(Boolean);
}}

async function pickExistingTag(tag) {{
  if (!tag || !tagPopoverSlug) return;
  await updatePersonTags(tagPopoverSlug, tag, false, true);
}}

function showNewTagInput(ev) {{
  if (ev) ev.stopPropagation();
  const pop = document.getElementById('tag_picker');
  if (!pop) return;
  pop.innerHTML = (
    '<div class="tag-picker-row tag-picker-new">'
    + '<input id="tag_new_input" type="text" placeholder="' + escapeHtmlAttr(t('people.tag.new_placeholder')) + '" autocomplete="off">'
    + '<button type="button" class="tag-picker-confirm" title="' + escapeHtmlAttr(t('people.tag.add_title')) + '">&#10003;</button>'
    + '</div>'
  );
  const input = document.getElementById('tag_new_input');
  if (!input) return;
  input.focus();
  input.addEventListener('keydown', function(e) {{
    if (e.key === 'Enter') {{ e.preventDefault(); confirmNewTagInput(); }}
    if (e.key === 'Escape') {{ e.preventDefault(); closeTagPicker(); }}
  }});
  requestAnimationFrame(function() {{ repositionTagPicker(); }});
}}

function resolveTagName(raw) {{
  const trimmed = String(raw || '').trim();
  if (!trimmed) return '';
  const lower = trimmed.toLowerCase();
  for (let i = 0; i < peopleAllTags.length; i++) {{
    if (String(peopleAllTags[i]).toLowerCase() === lower) return peopleAllTags[i];
  }}
  return trimmed;
}}

async function confirmNewTagInput() {{
  const input = document.getElementById('tag_new_input');
  const tag = resolveTagName(input ? input.value : '');
  if (!tag || !tagPopoverSlug) {{ closeTagPicker(); return; }}
  await updatePersonTags(tagPopoverSlug, tag, false, true);
}}

async function cycleTagConsent(ev, slug, tag) {{
  if (ev) ev.stopPropagation();
  const body = new URLSearchParams();
  body.set('name', slug);
  body.set('cycle', tag);
  try {{
    const r = await fetch('/api/people/tags', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: body.toString(),
    }});
    const d = await r.json();
    if (!d.ok) {{ alert(d.error || t('people.alert.tag_failed')); return; }}
    await applyPersonTagsUpdate(slug, d, {{}});
  }} catch (e) {{
    alert(t('people.alert.tag_failed'));
  }}
}}

async function removePersonTag(slug, tag) {{
  await updatePersonTags(slug, tag, true, false);
}}

async function updatePersonTags(slug, tag, remove, keepPickerOpen) {{
  const body = new URLSearchParams();
  body.set('name', slug);
  if (remove) body.set('remove', tag);
  else body.set('add', tag);
  try {{
    const r = await fetch('/api/people/tags', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: body.toString(),
    }});
    const d = await r.json();
    if (!d.ok) {{ alert(d.error || t('people.alert.tag_failed')); return null; }}
    if (!remove) {{
      const lower = String(tag).toLowerCase();
      let found = false;
      for (let i = 0; i < peopleAllTags.length; i++) {{
        if (String(peopleAllTags[i]).toLowerCase() === lower) {{ found = true; break; }}
      }}
      if (!found) {{
        peopleAllTags.push(tag);
        peopleAllTags.sort();
      }}
    }}
    await applyPersonTagsUpdate(slug, d, {{
      keepPickerOpen: !!(keepPickerOpen && tagPopoverSlug === slug),
    }});
    return d;
  }} catch (e) {{
    alert(t('people.alert.tag_failed'));
    return null;
  }}
}}

async function refreshPeopleTable(opts) {{
  const keepPicker = opts && opts.keepPickerOpen && tagPopoverSlug;
  const tbody = document.getElementById('people_table_body');
  if (!tbody) return;
  try {{
    const r = await fetch('/api/people_table');
    const d = await r.json();
    if (d.ok && d.html) tbody.innerHTML = d.html;
    if (d.ok && d.all_tags) peopleAllTags = d.all_tags;
    filterPeopleTable();
    applyPeopleTableSort();
    initTagsOverflow();
    if (keepPicker) reopenTagPickerContent();
    const show = !!(d.ok && d.has_mismatch);
    const navWarn = document.getElementById('nav_people_mismatch_warn');
    if (navWarn) navWarn.style.display = show ? 'inline' : 'none';
    const navPeople = document.getElementById('nav_people_link');
    if (navPeople) {{
      if (show) navPeople.title = t('nav.people_mismatch_tip');
      else navPeople.removeAttribute('title');
    }}
  }} catch (e) {{}}
}}

async function pollPeopleJob() {{
  const prog = document.getElementById('scan_progress');
  try {{
    const r = await fetch('/api/status');
    const d = await r.json();
    const p = d.progress_line || '';
    if (prog) prog.textContent = (d.stage || '') + (p ? ('  ' + p) : '');
    if (d.running) {{
      setTimeout(pollPeopleJob, 1000);
    }} else {{
      if (prog) prog.textContent = t('people.msg.finished_refresh');
      await refreshPeopleTable();
      if (prog) prog.textContent = '';
    }}
  }} catch (e) {{
    setTimeout(pollPeopleJob, 1500);
  }}
}}

async function scanPeople(ev) {{
  if (ev) ev.preventDefault();
  const el = document.getElementById('people_root_people');
  const res = document.getElementById('scan_result');
  const prog = document.getElementById('scan_progress');
  const folder = (el && el.value) || '';
  res.style.color = '#9aa4b2';
  res.textContent = t('people.msg.scanning');
  prog.textContent = '';
  try {{
    const r = await fetch('/api/scan_people', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: new URLSearchParams({{people_root: folder}}).toString()
    }});
    const d = await r.json();
    if (!d.ok) {{
      res.style.color = '#ef6b73';
      res.textContent = d.error || t('people.msg.scan_failed');
      return false;
    }}
    res.style.color = '#3cc087';
    res.textContent = d.message || t('common.done');
    if (d.registering || d.syncing) {{
      pollPeopleJob();
    }} else {{
      await refreshPeopleTable();
    }}
  }} catch (e) {{
    res.style.color = '#ef6b73';
    res.textContent = t('people.msg.scan_failed') + ': ' + e;
  }}
  return false;
}}

async function peopleConsentToggle(name, setAllowed) {{
  const res = document.getElementById('scan_result');
  const prog = document.getElementById('scan_progress');
  res.style.color = '#9aa4b2';
  res.textContent = t('people.msg.updating_consent');
  if (prog) prog.textContent = '';
  try {{
    const body = new URLSearchParams();
    body.set('name', name);
    body.set('set_to', setAllowed ? 'allowed' : 'blocked');
    const r = await fetch('/api/people/consent', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: body.toString()
    }});
    const d = await r.json();
    if (!d.ok) {{
      res.style.color = '#ef6b73';
      res.textContent = d.error || t('people.msg.update_failed');
      return false;
    }}
    res.style.color = '#3cc087';
    res.textContent = d.message || t('common.done');
    if (d.syncing) pollPeopleJob();
    else await refreshPeopleTable();
  }} catch (e) {{
    res.style.color = '#ef6b73';
    res.textContent = t('people.msg.update_failed') + ': ' + e;
  }}
  return false;
}}

async function peopleWipeByName(name) {{
  if (!confirm(t('people.confirm.wipe_short', {{name: name}}))) return false;
  const res = document.getElementById('scan_result');
  const prog = document.getElementById('scan_progress');
  res.style.color = '#9aa4b2';
  res.textContent = t('people.msg.wiping');
  if (prog) prog.textContent = '';
  try {{
    const r = await fetch('/api/people/delete', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: new URLSearchParams({{name: name}}).toString()
    }});
    const d = await r.json();
    if (!d.ok) {{
      res.style.color = '#ef6b73';
      res.textContent = d.error || t('people.msg.delete_failed');
      return false;
    }}
    res.style.color = '#3cc087';
    res.textContent = d.message || t('common.done');
    if (d.syncing) pollPeopleJob();
    else await refreshPeopleTable();
  }} catch (e) {{
    res.style.color = '#ef6b73';
    res.textContent = t('people.msg.delete_failed') + ': ' + e;
  }}
  return false;
}}

async function peopleConsent(ev) {{
  ev.preventDefault();
  const res = document.getElementById('scan_result');
  const prog = document.getElementById('scan_progress');
  res.style.color = '#9aa4b2';
  res.textContent = t('people.msg.updating_consent');
  if (prog) prog.textContent = '';
  try {{
    const r = await fetch('/api/people/consent', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: new URLSearchParams(new FormData(ev.target)).toString()
    }});
    const d = await r.json();
    if (!d.ok) {{
      res.style.color = '#ef6b73';
      res.textContent = d.error || t('people.msg.update_failed');
      return false;
    }}
    res.style.color = '#3cc087';
    res.textContent = d.message || t('common.done');
    if (d.syncing) pollPeopleJob();
    else await refreshPeopleTable();
  }} catch (e) {{
    res.style.color = '#ef6b73';
    res.textContent = t('people.msg.update_failed') + ': ' + e;
  }}
  return false;
}}

async function peopleDelete(ev, name) {{
  ev.preventDefault();
  if (!confirm(t('people.confirm.wipe_long', {{name: name}}))) return false;
  const res = document.getElementById('scan_result');
  const prog = document.getElementById('scan_progress');
  res.style.color = '#9aa4b2';
  res.textContent = t('people.msg.wiping');
  if (prog) prog.textContent = '';
  try {{
    const r = await fetch('/api/people/delete', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: new URLSearchParams(new FormData(ev.target)).toString()
    }});
    const d = await r.json();
    if (!d.ok) {{
      res.style.color = '#ef6b73';
      res.textContent = d.error || t('people.msg.delete_failed');
      return false;
    }}
    res.style.color = '#3cc087';
    res.textContent = d.message || t('common.done');
    if (d.syncing) pollPeopleJob();
    else await refreshPeopleTable();
  }} catch (e) {{
    res.style.color = '#ef6b73';
    res.textContent = t('people.msg.delete_failed') + ': ' + e;
  }}
  return false;
}}

async function reregisterPerson(name) {{
  if (!confirm(t('people.confirm.reregister', {{name: name}}))) {{
    return false;
  }}
  const res = document.getElementById('scan_result');
  const prog = document.getElementById('scan_progress');
  res.style.color = '#9aa4b2';
  res.textContent = t('people.msg.reregistering', {{name: name}});
  prog.textContent = '';
  try {{
    const r = await fetch('/api/reregister_person', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: new URLSearchParams({{name: name}}).toString()
    }});
    const d = await r.json();
    if (!d.ok) {{
      res.style.color = '#ef6b73';
      res.textContent = d.error || t('people.msg.reregister_failed');
      return false;
    }}
    res.style.color = '#3cc087';
    res.textContent = d.message || t('common.done');
    if (d.syncing) pollPeopleJob();
    else await refreshPeopleTable();
  }} catch (e) {{
    res.style.color = '#ef6b73';
    res.textContent = t('people.msg.reregister_failed') + ': ' + e;
  }}
  return false;
}}

async function reregisterAllPeople() {{
  if (!confirm(t('people.confirm.reregister_all'))) {{
    return false;
  }}
  const res = document.getElementById('scan_result');
  const prog = document.getElementById('scan_progress');
  res.style.color = '#9aa4b2';
  res.textContent = t('people.msg.reregistering_all');
  prog.textContent = '';
  try {{
    const r = await fetch('/api/reregister_all_people', {{ method: 'POST' }});
    const d = await r.json();
    if (!d.ok) {{
      res.style.color = '#ef6b73';
      res.textContent = d.error || t('people.msg.reregister_failed');
      return false;
    }}
    res.style.color = '#3cc087';
    res.textContent = d.message || t('common.done');
    if (d.syncing) pollPeopleJob();
    else await refreshPeopleTable();
  }} catch (e) {{
    res.style.color = '#ef6b73';
    res.textContent = t('people.msg.reregister_failed') + ': ' + e;
  }}
  return false;
}}

function escapeHtmlAttr(s) {{
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;');
}}

function bindGalleryGridClicks(grid) {{
  if (!grid || grid.dataset.bound === '1') return;
  grid.dataset.bound = '1';
  grid.addEventListener('click', function(ev) {{
    const img = ev.target.closest('img.gallery-thumb');
    if (!img) return;
    const idx = parseInt(img.dataset.idx || '-1', 10);
    if (idx >= 0) showHeroAtIndex(idx);
  }});
}}

async function openGallery(name) {{
  const modal = document.getElementById('gallery_modal');
  const title = document.getElementById('gallery_title');
  const meta = document.getElementById('gallery_meta');
  const grid = document.getElementById('gallery_grid');
  const hero = document.getElementById('gallery_hero');
  const srcEl = document.getElementById('gallery_source');
  const matchEl = document.getElementById('gallery_match');
  const delBtn = document.getElementById('gallery_delete_btn');
  galleryPersonName = name;
  gallerySelectedPath = '';
  galleryPhotos = [];
  galleryPhotoIndex = -1;
  title.textContent = name;
  meta.textContent = t('people.gallery.loading');
  grid.innerHTML = '';
  hero.innerHTML = '';
  if (srcEl) srcEl.textContent = '';
  if (matchEl) matchEl.textContent = '';
  if (delBtn) delBtn.style.display = 'none';
  updateGalleryNavUi();
  modal.classList.add('open');
  bindGalleryGridClicks(grid);
  try {{
    const r = await fetch('/api/person_photos?name=' + encodeURIComponent(name));
    const d = await r.json();
    if (!d.ok) {{
      meta.textContent = d.error || t('people.gallery.load_failed');
      return false;
    }}
    galleryPhotos = d.photos || [];
    meta.textContent = galleryPhotos.length
      ? t('people.gallery.count_hint', {{n: galleryPhotos.length}})
      : t('people.gallery.empty');
    grid.innerHTML = galleryPhotos.map(function(p, idx) {{
      const url = escapeHtmlAttr(p.url);
      const path = escapeHtmlAttr(p.path);
      const fname = escapeHtmlAttr(p.name);
      const score = (p.match_score != null && !isNaN(Number(p.match_score)))
        ? Number(p.match_score)
        : null;
      const scoreLabel = (score != null) ? score.toFixed(1) : '';
      const titleBits = [p.name];
      if (score != null) titleBits.push(t('people.gallery.match_score', {{n: scoreLabel}}));
      const titleAttr = escapeHtmlAttr(titleBits.join(' — '));
      const badge = (score != null)
        ? ('<span class="gallery-score-badge">' + escapeHtml(scoreLabel) + '</span>')
        : '';
      return (
        '<div class="gallery-thumb-wrap">' +
          '<img class="gallery-thumb" src="' + url + '" alt="' + fname + '" title="' + titleAttr + '" '
          + 'data-idx="' + idx + '" data-url="' + url + '" data-path="' + path + '" data-name="' + fname + '">' +
          badge +
        '</div>'
      );
    }}).join('');
    if (galleryPhotos.length) showHeroAtIndex(0);
    else updateGalleryNavUi();
  }} catch (e) {{
    meta.textContent = t('people.gallery.failed', {{e: e}});
  }}
  return false;
}}

let galleryPersonName = '';
let gallerySelectedPath = '';
let gallerySelectedName = '';
let galleryPhotos = [];
let galleryPhotoIndex = -1;

function updateGalleryNavUi() {{
  const pos = document.getElementById('gallery_pos_label');
  if (pos) {{
    pos.textContent = (galleryPhotos.length && galleryPhotoIndex >= 0)
      ? ((galleryPhotoIndex + 1) + ' / ' + galleryPhotos.length)
      : '';
  }}
}}

function showHeroAtIndex(idx) {{
  if (idx < 0 || idx >= galleryPhotos.length) return;
  galleryPhotoIndex = idx;
  const p = galleryPhotos[idx];
  showHero(p.url, p.path, p.name, p.source_path || null, p.match_score);
  updateGalleryNavUi();
}}

function galleryNav(delta) {{
  showHeroAtIndex(galleryPhotoIndex + delta);
}}

function showHero(url, path, fileName, sourcePath, matchScore) {{
  gallerySelectedPath = path || '';
  gallerySelectedName = fileName || '';
  const safeUrl = escapeHtmlAttr(url || '');
  document.getElementById('gallery_hero').innerHTML = '<img src="' + safeUrl + '" alt="' + escapeHtmlAttr(t('common.preview_alt')) + '">';
  const srcEl = document.getElementById('gallery_source');
  if (srcEl) {{
    const sp = (sourcePath || '').trim();
    srcEl.textContent = sp || t('people.gallery.source_unknown');
  }}
  const matchEl = document.getElementById('gallery_match');
  if (matchEl) {{
    if (matchScore != null && !isNaN(Number(matchScore))) {{
      matchEl.textContent = t('people.gallery.match_score', {{n: Number(matchScore).toFixed(1)}});
    }} else {{
      matchEl.textContent = '';
    }}
  }}
  const delBtn = document.getElementById('gallery_delete_btn');
  if (delBtn) delBtn.style.display = gallerySelectedPath ? 'inline-block' : 'none';
  document.querySelectorAll('#gallery_grid img.gallery-thumb').forEach(function(el) {{
    const active = (galleryPhotoIndex >= 0)
      ? (parseInt(el.dataset.idx || '-1', 10) === galleryPhotoIndex)
      : (el.dataset.path === gallerySelectedPath);
    el.style.borderColor = active ? 'var(--accent)' : '';
    el.style.boxShadow = active ? '0 0 0 2px var(--accent)' : '';
  }});
}}

async function deleteGalleryPhoto() {{
  if (!galleryPersonName || !gallerySelectedPath) return false;
  const label = gallerySelectedName || gallerySelectedPath.split('/').pop();
  if (!confirm(t('people.gallery.confirm_delete', {{label: label, person: galleryPersonName}}))) {{
    return false;
  }}
  const meta = document.getElementById('gallery_meta');
  try {{
    const r = await fetch('/api/person_photo/delete', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: new URLSearchParams({{
        name: galleryPersonName,
        path: gallerySelectedPath,
      }}).toString(),
    }});
    const d = await r.json();
    if (!d.ok) {{
      meta.style.color = '#ef6b73';
      meta.textContent = d.error || t('people.gallery.delete_failed');
      return false;
    }}
    meta.style.color = '#3cc087';
    meta.textContent = d.message || t('people.gallery.deleted');
    await openGallery(galleryPersonName);
    await refreshPeopleTable();
  }} catch (e) {{
    meta.style.color = '#ef6b73';
    meta.textContent = t('people.gallery.delete_failed') + ': ' + e;
  }}
  return false;
}}

function closeGallery() {{
  document.getElementById('gallery_modal').classList.remove('open');
  galleryPhotos = [];
  galleryPhotoIndex = -1;
}}

document.addEventListener('keydown', function(e) {{
  const modal = document.getElementById('gallery_modal');
  const open = modal && modal.classList.contains('open');
  if (e.key === 'Escape' && open) {{
    closeGallery();
    return;
  }}
  if (!open) return;
  if (e.key === 'ArrowLeft') {{ e.preventDefault(); galleryNav(-1); }}
  if (e.key === 'ArrowRight') {{ e.preventDefault(); galleryNav(1); }}
}});

async function stopServer() {{
  if (!confirm(t('common.confirm.stop_server'))) return;
  const r = await fetch('/shutdown', {{ method: 'POST' }});
  const d = await r.json();
  const msg = d.message || t('common.shutdown.fallback_msg');
  document.body.innerHTML = `
    <div style="display:flex;min-height:100vh;align-items:center;justify-content:center;background:#0f1115;color:#e7ebf3;font-family:Inter,-apple-system,sans-serif;">
      <div style="max-width:520px;padding:24px;border:1px solid #2a3140;border-radius:12px;background:#171a21;">
        <h2 style="margin:0 0 10px;">${{t('common.shutdown.title')}}</h2>
        <p style="margin:0 0 12px;color:#9aa4b2;">${{msg}}</p>
        <p style="margin:0;color:#9aa4b2;">${{t('common.shutdown.close_tab_restart')}}</p>
      </div>
    </div>
  `;
}}

(async function() {{
  applyPeopleTableSort();
  window.addEventListener('resize', function() {{
    initTagsOverflow();
    repositionTagPicker();
  }});
  window.addEventListener('scroll', function() {{ repositionTagPicker(); }}, true);
  try {{
    const r = await fetch('/api/status');
    const d = await r.json();
    if (d.running) pollPeopleJob();
  }} catch (e) {{}}
}})();
</script>
</div></body></html>"""
            self._send_html(body)
            return
        if parsed.path != "/":
            self._send_html(html.escape(_t("common.not_found", self._lang())), 404)
            return
        lang = self._lang()
        snap = STATE.snapshot()
        analyze_folder_last = html.escape(str(snap.get("analyze_folder_last", "")))
        summary = snap.get("summary") or {}
        status_counts = snap.get("status_counts") or {}
        counts_ready = bool(snap.get("status_counts_ready"))

        def _status_metric(key: str) -> str:
            if snap.get("running") and key != "files_found":
                return "-"
            v = summary.get(key)
            return html.escape(str(v)) if v is not None else "-"

        def _outcome_metric(key: str) -> str:
            if snap.get("running") or not counts_ready:
                return "-"
            v = status_counts.get(key)
            return html.escape(str(v)) if v is not None else "-"

        def _outcome_metric_html(key: str, status: str) -> str:
            text = _outcome_metric(key)
            if text == "-":
                return text
            folder = str(snap.get("analyze_folder_last") or "").strip()
            try:
                n = int(status_counts.get(key) or 0)
            except (TypeError, ValueError):
                return text
            if n <= 0 or not folder:
                return text
            href = f"/review?folder={quote(folder)}&status={status}"
            return f'<a class="person-link" href="{html.escape(href)}">{text}</a>'

        init_status = html.escape(str(snap.get("status") if not snap.get("running") else "Running"))
        init_badge_class = "running" if snap.get("running") else (
            "fail" if snap.get("status") == "Failed" else
            "warn" if snap.get("status") in ("Stopped", "Completed with warnings") else
            "done" if snap.get("status") == "Completed" else "idle"
        )

        init_files = _status_metric("files_found")
        init_newly = _status_metric("newly_analyzed")
        init_decode = _status_metric("decode_errors")
        init_blocked = _outcome_metric_html("blocked", "blocked")
        init_review = _outcome_metric_html("review", "review")
        init_ok = _outcome_metric("ok")
        init_progress = html.escape(str(snap.get("progress_line") or "-")) if not snap.get("running") else "-"
        init_stage = html.escape(str(snap.get("stage") or "Waiting"))
        cfg = _read_config_form()
        ingest_enabled = bool(cfg.get("ingest_enabled"))
        ingest_dest_val = html.escape(str(snap.get("ingest_dest_last") or ""))
        analyze_paths_html = f"""
<div class="form-row">
  <span class="form-label">{html.escape(_t("analyze.label.source", lang))}</span>
  <div class="form-control">
    <div class="input-with-clear">
      <input id="search_folder" type="text" name="folder" placeholder="{html.escape(_t("analyze.placeholder.source", lang))}" value="{analyze_folder_last}">
      <button type="button" class="input-clear" data-clear-for="search_folder" aria-label="{html.escape(_t("common.clear", lang))}" hidden>&times;</button>
    </div>
    <button type="button" onclick="pickFolder('search_folder')">{html.escape(_t("common.choose_finder", lang))}</button>
  </div>
</div>"""
        if ingest_enabled:
            analyze_paths_html += f"""
<div class="form-row">
  <span class="form-label">{html.escape(_t("analyze.label.destination", lang))}</span>
  <div class="form-control">
    <div class="input-with-clear">
      <input id="ingest_destination" type="text" name="ingest_destination" placeholder="{html.escape(_t("analyze.placeholder.destination", lang))}" value="{ingest_dest_val}" required>
      <button type="button" class="input-clear" data-clear-for="ingest_destination" aria-label="{html.escape(_t("common.clear", lang))}" hidden>&times;</button>
    </div>
    <button type="button" onclick="pickFolder('ingest_destination')">{html.escape(_t("common.choose_finder", lang))}</button>
  </div>
</div>"""
        body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(_t("app.title.analyze", lang))}</title>
{self._base_style()}
</head><body>
{self._header("analyze", "subtitle.analyze")}
<div>
<fieldset><legend>{html.escape(_t("analyze.legend", lang))}</legend>
<form id="analyze_form" method="post" action="/analyze">
{analyze_paths_html}
<button type="button" id="analyze_toggle" onclick="toggleAnalyze()">{html.escape(_t("analyze.btn.start", lang))}</button>
</form></fieldset>
</div>
<fieldset><legend>{html.escape(_t("analyze.status.legend", lang))}</legend>
  <div><span id="status_badge" class="badge {init_badge_class}">{init_status}</span> <span id="stage_text" style="margin-left:8px;color:var(--muted)">{init_stage}</span></div>
  <div class="status-grid">
    <div class="metric"><div class="k">{html.escape(_t("analyze.metric.files_found", lang))}</div><div id="s_files_found" class="v">{init_files}</div></div>
    <div class="metric"><div class="k">{html.escape(_t("analyze.metric.newly_analyzed", lang))}</div><div id="s_newly_analyzed" class="v">{init_newly}</div></div>
    <div class="metric"><div class="k">{html.escape(_t("analyze.metric.unreadable", lang))}</div><div id="s_decode_errors" class="v">{init_decode}</div></div>
  </div>
  <div class="status-grid">
    <div class="metric"><div class="k">{html.escape(_t("analyze.metric.blocked", lang))}</div><div id="o_blocked" class="v">{init_blocked}</div></div>
    <div class="metric"><div class="k">{html.escape(_t("analyze.metric.review", lang))}</div><div id="o_review" class="v">{init_review}</div></div>
    <div class="metric"><div class="k">{html.escape(_t("analyze.metric.ok", lang))}</div><div id="o_ok" class="v">{init_ok}</div></div>
  </div>
  <div style="margin-top:10px;">
    <div class="group-title" style="margin:0 0 6px;">{html.escape(_t("analyze.progress.title", lang))}</div>
    <div id="progress_line" style="background:#121721;border:1px solid var(--line);border-radius:10px;padding:10px 12px;color:var(--text);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.4;">
      {init_progress}
    </div>
  </div>
  <div style="margin-top:10px;">
    <div class="group-title" style="margin:0 0 6px;">{html.escape(_t("analyze.active_runs.title", lang))}</div>
    <div id="active_runs_meta" style="color:var(--muted);font-size:12px;margin-bottom:6px;"></div>
    <div id="active_runs" style="color:var(--muted);font-size:13px;white-space:pre-line;">-</div>
  </div>
</fieldset>
<fieldset><legend>{html.escape(_t("analyze.activity.legend", lang))}</legend>
<pre id="activity"></pre>
</fieldset>
<fieldset><legend>{html.escape(_t("analyze.log.legend", lang))} <button type="button" class="btn-small btn-muted" id="copy_log_btn" onclick="copyTechnicalLog()">{html.escape(_t("analyze.log.copy", lang))}</button></legend>
<details>
  <summary>{html.escape(_t("analyze.log.show", lang))}</summary>
  <pre id="log"></pre>
</details>
</fieldset>
<script>
function syncAnalyzeClearButton(inputId) {{
  const input = document.getElementById(inputId);
  const btn = document.querySelector('#analyze_form .input-clear[data-clear-for="' + inputId + '"]');
  if (!input || !btn) return;
  btn.hidden = !(input.value || '').trim();
}}

function initAnalyzeClearButtons() {{
  ['search_folder', 'ingest_destination'].forEach(function(id) {{
    const el = document.getElementById(id);
    if (!el) return;
    const normalize = function() {{
      el.value = normalizeFolderPath(el.value);
      syncAnalyzeClearButton(id);
    }};
    el.addEventListener('input', normalize);
    el.addEventListener('change', normalize);
    el.addEventListener('paste', function() {{ setTimeout(normalize, 0); }});
    syncAnalyzeClearButton(id);
  }});
  document.querySelectorAll('#analyze_form .input-clear[data-clear-for]').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      const id = btn.getAttribute('data-clear-for');
      const el = document.getElementById(id);
      if (!el) return;
      el.value = '';
      syncAnalyzeClearButton(id);
      el.focus();
    }});
  }});
}}

initAnalyzeClearButtons();

(function() {{
  const params = new URLSearchParams(location.search);
  if (params.get('folder_error') === '1') {{
    alert(t('analyze.alert.folder_not_found'));
    history.replaceState(null, '', location.pathname);
  }}
}})();

async function copyTechnicalLog() {{
  const el = document.getElementById('log');
  const text = el ? (el.textContent || '') : '';
  const btn = document.getElementById('copy_log_btn');
  const setLabel = (label) => {{ if (btn) btn.textContent = label; }};
  try {{
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      await navigator.clipboard.writeText(text);
    }} else {{
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }}
    setLabel(t('analyze.log.copied'));
    setTimeout(() => setLabel(t('analyze.log.copy')), 1500);
  }} catch (e) {{
    alert(t('analyze.alert.copy_failed'));
  }}
}}

async function stopServer() {{
  if (!confirm(t('common.confirm.stop_server'))) return;
  const r = await fetch('/shutdown', {{ method: 'POST' }});
  const d = await r.json();
  const msg = d.message || t('common.shutdown.fallback_msg');
  document.body.innerHTML = `
    <div style="display:flex;min-height:100vh;align-items:center;justify-content:center;background:#0f1115;color:#e7ebf3;font-family:Inter,-apple-system,sans-serif;">
      <div style="max-width:520px;padding:24px;border:1px solid #2a3140;border-radius:12px;background:#171a21;">
        <h2 style="margin:0 0 10px;">${{t('common.shutdown.title')}}</h2>
        <p style="margin:0 0 12px;color:#9aa4b2;">${{msg}}</p>
        <p style="margin:0;color:#9aa4b2;">${{t('common.shutdown.close_tab_restart')}}</p>
      </div>
    </div>
  `;
}}

function updateAnalyzeToggle(running) {{
  const btn = document.getElementById('analyze_toggle');
  if (!btn) return;
  if (running) {{
    btn.textContent = t('analyze.btn.stop');
    btn.classList.add('btn-stop');
  }} else {{
    btn.textContent = t('analyze.btn.start');
    btn.classList.remove('btn-stop');
  }}
}}

function clearAnalyzeStatus() {{
  const dash = '-';
  ['files_found','newly_analyzed','decode_errors'].forEach(function(k) {{
    const el = document.getElementById('s_' + k);
    if (el) el.textContent = dash;
  }});
  ['o_blocked','o_review','o_ok'].forEach(function(id) {{
    const el = document.getElementById(id);
    if (el) el.textContent = dash;
  }});
  const prog = document.getElementById('progress_line');
  if (prog) prog.textContent = dash;
  const act = document.getElementById('activity');
  if (act) act.textContent = '';
  const badge = document.getElementById('status_badge');
  if (badge) {{ badge.textContent = 'Running'; badge.className = 'badge running'; }}
  const stage = document.getElementById('stage_text');
  if (stage) stage.textContent = t('analyze.stage.preparing_ellipsis');
}}

function setOutcomeCount(elId, statusKey, count, linkable) {{
  const el = document.getElementById(elId);
  if (!el) return;
  if (!linkable || count === undefined || count === null) {{
    el.textContent = (count === undefined || count === null) ? '-' : String(count);
    return;
  }}
  const n = Number(count);
  const folder = ((document.getElementById('search_folder') || {{}}).value || '').trim();
  if (!Number.isFinite(n) || n <= 0 || !folder) {{
    el.textContent = String(count);
    return;
  }}
  const a = document.createElement('a');
  a.className = 'person-link';
  a.href = '/review?folder=' + encodeURIComponent(folder) + '&status=' + encodeURIComponent(statusKey);
  a.textContent = String(n);
  el.replaceChildren(a);
}}

async function toggleAnalyze() {{
  const r = await fetch('/api/status');
  const d = await r.json();
  if (d.running) {{
    if (!confirm(t('analyze.confirm.stop_run'))) return;
    const sr = await fetch('/api/stop_run', {{ method: 'POST' }});
    const sd = await sr.json();
    if (!sd.ok) {{
      alert(sd.error || t('analyze.alert.stop_failed'));
      return;
    }}
    updateAnalyzeToggle(true);
    return;
  }}
  const folderEl = document.getElementById('search_folder');
  let folder = folderEl ? normalizeFolderPath(folderEl.value) : '';
  if (folderEl) folderEl.value = folder;
  if (!folder.trim()) {{
    alert(t('analyze.alert.no_folder'));
    return;
  }}
  const ingestDestEl = document.getElementById('ingest_destination');
  if (ingestDestEl) {{
    ingestDestEl.value = normalizeFolderPath(ingestDestEl.value);
    if (!ingestDestEl.value.trim()) {{
      alert(t('analyze.alert.no_dest'));
      ingestDestEl.focus();
      return;
    }}
  }}
  clearAnalyzeStatus();
  document.getElementById('analyze_form').submit();
}}

async function poll() {{
  const r = await fetch('/api/status');
  const d = await r.json();
  document.getElementById('server_state').textContent = d.running ? 'Running' : d.status;
  document.getElementById('server_state').className = 'badge ' + (
    d.status === 'Failed' ? 'fail' :
    d.status === 'Stopped' ? 'warn' :
    d.status === 'Completed with warnings' ? 'warn' :
    d.status === 'Completed' ? 'done' :
    d.running ? 'running' : 'idle'
  );
  document.getElementById('status_badge').textContent = d.status;
  document.getElementById('status_badge').className = document.getElementById('server_state').className;
  document.getElementById('stage_text').textContent = d.stage || 'Waiting';
  updateAnalyzeToggle(!!d.running);
  const s = d.summary || {{}};
  const keys = ['files_found','newly_analyzed','decode_errors'];
  for (const k of keys) {{
    const el = document.getElementById('s_' + k);
    if (el) {{
      if (d.running && k !== 'files_found') {{
        el.textContent = '-';
        continue;
      }}
      const v = s[k];
      el.textContent = (v === undefined || v === null) ? '-' : v.toString();
    }}
  }}
  const sc = d.status_counts || {{}};
  const showCounts = !!d.status_counts_ready && !d.running;
  setOutcomeCount('o_blocked', 'blocked', showCounts ? (sc.blocked ?? null) : null, showCounts);
  setOutcomeCount('o_review', 'review', showCounts ? (sc.review ?? null) : null, showCounts);
  setOutcomeCount('o_ok', 'ok', showCounts ? (sc.ok ?? null) : null, false);
  document.getElementById('activity').textContent = (d.activity || []).join('\\n');
  document.getElementById('log').textContent = (d.logs || []).join('\\n');
  const p = d.progress_line || '';
  document.getElementById('progress_line').textContent = p || '-';
  const runs = d.active_runs || [];
  const ar = document.getElementById('active_runs');
  const meta = document.getElementById('active_runs_meta');
  if (meta) {{
    const idn = d.db_identity || {{}};
    const host = d.this_host || '';
    const detail = idn.detail || idn.label || '-';
    let metaText = t('analyze.active_runs.db', {{detail: detail}});
    if (host) metaText += '\\n' + t('analyze.active_runs.this_host', {{host: host}});
    if (idn.backend === 'sqlite' || idn.shared === false) {{
      metaText += '\\n' + t('analyze.active_runs.hint_sqlite');
    }}
    meta.textContent = metaText;
    meta.style.color = (idn.backend === 'sqlite' || idn.shared === false) ? '#ef6b73' : 'var(--muted)';
  }}
  if (ar) {{
    if (!runs.length) {{
      ar.textContent = t('analyze.active_runs.empty');
    }} else {{
      const me = d.this_host || '';
      const markLabel = t('analyze.active_runs.this_pc_mark');
      ar.textContent = runs.map(function(r) {{
        const mark = (me && r.host === me) ? markLabel : '';
        return r.host + ' -> ' + (r.folder_name || r.folder) + mark;
      }}).join('\\n');
    }}
  }}
}}
setInterval(poll, 1000); poll();
</script>
</div></body></html>"""
        self._send_html(body)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        lang = lang_from_cookie_header(self.headers.get("Cookie"))
        if parsed.path in ("/api/people/create", "/api/people/update"):
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                self._send_json({"ok": False, "error": _t("api.expected_multipart", lang)})
                return
            fs = self._parse_multipart()
            if parsed.path == "/api/people/create":
                self._send_json(_create_person_request(fs, lang=lang))
            else:
                self._send_json(_update_person_request(fs, lang=lang))
            return
        form = self._parse_form()
        if parsed.path == "/api/people/ensure_folder":
            self._send_json(
                _ensure_person_folder_request(
                    form.get("name", ""),
                    form.get("display_name", ""),
                    lang=lang,
                )
            )
            return
        if parsed.path == "/shutdown":
            STATE.add_log("Shutdown requested from UI.")
            self._send_json({"ok": True, "message": _t("api.shutdown", lang)})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if parsed.path == "/api/set_people_root":
            root_text = form.get("people_root", "").strip()
            if root_text and Path(root_text).expanduser().is_dir():
                resolved = str(Path(root_text).expanduser().resolve())
                STATE.set_last_paths(people_root=resolved)
                _persist_people_dir(resolved)
                self._send_json({"ok": True, "path": resolved})
            else:
                self._send_json({"ok": False, "error": _t("api.invalid_people_folder_short", lang)})
            return
        if parsed.path == "/api/reregister_person":
            self._send_json(_reregister_person(form.get("name", ""), lang=lang))
            return
        if parsed.path == "/api/reregister_all_people":
            self._send_json(_reregister_all_people(lang=lang))
            return
        if parsed.path == "/api/stop_run":
            self._send_json(_stop_run(lang=lang))
            return
        if parsed.path == "/api/scan_people":
            root_text = form.get("people_root", "")
            STATE.set_last_paths(people_root=root_text)
            self._send_json(_scan_people_plan_and_start(root_text, lang=lang))
            return
        if parsed.path == "/scan_people":
            # Non-JS fallback: run scan and stay on the People page.
            STATE.set_last_paths(people_root=form.get("people_root", ""))
            _scan_people_root(form.get("people_root", ""))
            self._redirect("/people")
            return
        if parsed.path == "/api/person_photo/delete":
            self._send_json(
                _delete_person_gallery_file(
                    form.get("name", "").strip(),
                    form.get("path", "").strip(),
                    lang=lang,
                )
            )
            return
        if parsed.path == "/api/review_photo/confirm_blocked":
            try:
                asset_id = int(form.get("asset_id", "0").strip())
            except ValueError:
                self._send_json({"ok": False, "error": _t("api.invalid_asset", lang)})
                return
            self._send_json(
                _confirm_review_blocked_request(
                    asset_id,
                    form.get("folder", "").strip(),
                    form.get("faces", "").strip(),
                    status=form.get("status", "review").strip(),
                    lang=lang,
                )
            )
            return
        if parsed.path == "/api/review_photo/save_assignments":
            try:
                asset_id = int(form.get("asset_id", "0").strip())
            except ValueError:
                self._send_json({"ok": False, "error": _t("api.invalid_asset", lang)})
                return
            self._send_json(
                _save_review_face_assignments_request(
                    asset_id,
                    form.get("folder", "").strip(),
                    form.get("faces", "").strip(),
                    form.get("status", "review").strip(),
                    lang=lang,
                )
            )
            return
        if parsed.path == "/api/review_photo/batch_blocked":
            self._send_json(_batch_confirm_review_blocked_request(form.get("folder", "").strip(), lang=lang))
            return
        if parsed.path == "/api/review_photo/confirm_ok":
            try:
                asset_id = int(form.get("asset_id", "0").strip())
            except ValueError:
                self._send_json({"ok": False, "error": _t("api.invalid_asset", lang)})
                return
            from_status = _parse_review_status(form.get("from_status", "blocked"))
            if from_status == "review":
                self._send_json(
                    _confirm_review_ok_request(
                        asset_id,
                        form.get("folder", "").strip(),
                        lang=lang,
                    )
                )
            else:
                self._send_json(
                    _confirm_blocked_ok_request(
                        asset_id,
                        form.get("folder", "").strip(),
                        lang=lang,
                    )
                )
            return
        if parsed.path == "/api/people/consent":
            name = form.get("name", "").strip()
            set_to = form.get("set_to", "").strip().lower()
            if not name or set_to not in ("allowed", "blocked"):
                self._send_json({"ok": False, "error": _t("api.invalid_consent", lang)})
                return
            self._send_json(
                _start_consent_update(name, consent_allowed=(set_to == "allowed"), lang=lang)
            )
            return
        if parsed.path == "/api/people/delete":
            self._send_json(_start_wipe_embeddings(form.get("name", ""), lang=lang))
            return
        if parsed.path == "/api/people/tags":
            self._send_json(_update_person_tags_request(form, lang=lang))
            return
        if parsed.path == "/people/consent":
            name = form.get("name", "").strip()
            set_to = form.get("set_to", "").strip().lower()
            if name and set_to in ("allowed", "blocked"):
                _start_consent_update(name, consent_allowed=(set_to == "allowed"))
            self._redirect("/people")
            return
        if parsed.path == "/people/delete":
            name = form.get("name", "").strip()
            if name:
                _start_wipe_embeddings(name)
            self._redirect("/people")
            return
        if parsed.path == "/analyze":
            if STATE.snapshot()["running"]:
                STATE.add_log("[warn] analyze already running — ignoring duplicate start")
                self._redirect_home()
                return
            folder = _normalize_folder_path(form.get("folder", ""))
            folder_path = Path(folder) if folder else None
            if folder_path is None or not folder_path.is_dir():
                STATE.add_log(f"[error] Analyze folder not found: {folder!r}")
                self._redirect("/?folder_error=1")
                return
            STATE.set_last_paths(analyze_folder=folder)
            cfg = _read_config_form()
            # Orphaned claim after Stop/kill (CLI finally may not run) — free this host only.
            n_released = _release_own_folder_claim(
                folder, message="cleared before new analyze from web UI"
            )
            ingest_dest = ""
            if cfg.get("ingest_enabled"):
                ingest_dest = _normalize_folder_path(form.get("ingest_destination", ""))
                if not ingest_dest:
                    STATE.add_log(
                        "[warn] Archive copy enabled but no destination path — analyze will skip archive"
                    )
                elif ingest_dest:
                    STATE.set_last_paths(ingest_dest=ingest_dest)
                    if cfg.get("ingest_order", "copy_then_analyze") == "copy_then_analyze":
                        try:
                            nas_scan = str(
                                resolve_ingest_destination(Path(folder), Path(ingest_dest))
                            )
                            n_nas = _release_own_folder_claim(
                                nas_scan, message="cleared before new analyze from web UI"
                            )
                            n_released += n_nas
                        except (ValueError, OSError) as e:
                            STATE.add_log(
                                f"[warn] could not resolve archive path for claim release: {e}"
                            )
            if n_released:
                STATE.add_log(
                    f"[warn] released {n_released} stale folder claim(s) before Analyze"
                )
            export_mode = str(cfg.get("export_flagged") or "off")
            flagged_statuses: list[str] = []
            if cfg.get("export_status_blocked"):
                flagged_statuses.append("blocked")
            if cfg.get("export_status_review"):
                flagged_statuses.append("review")
            # CLI falls back to both statuses when --flagged-status is omitted;
            # with none selected, disable export instead of exporting everything.
            if not flagged_statuses:
                export_mode = "off"
            force = bool(cfg.get("force_default"))
            sync_meta = bool(cfg.get("sync_metadata_default", False))
            cmd = [
                _cli_path("analyze_photos"),
                folder,
                "--usage",
                "social",
                "--export-flagged",
                export_mode,
            ]
            if force:
                cmd.append("--force")
            cmd.extend(["--sync-metadata"] if sync_meta else ["--no-sync-metadata"])
            for status in flagged_statuses:
                cmd.extend(["--flagged-status", status])
            people_root = str(STATE.people_root_last or "").strip()
            if people_root:
                cmd.extend(["--collect-to", people_root])
            if cfg.get("collect_crop_portrait"):
                cmd.append("--collect-crop")
            else:
                cmd.append("--no-collect-crop")
            if cfg.get("ingest_enabled") and ingest_dest:
                cmd.extend(["--ingest-to", ingest_dest])
                order = str(cfg.get("ingest_order") or "copy_then_analyze")
                cli_order = (
                    "analyze-then-copy"
                    if order == "analyze_then_copy"
                    else "copy-then-analyze"
                )
                cmd.extend(["--ingest-order", cli_order])
            elif not cfg.get("ingest_enabled"):
                cmd.append("--no-ingest")
            _prepare_analyze_run(folder)
            _run_commands("Analyze", [cmd], skip_reset=True)
            self._redirect_home()
            return
        if parsed.path == "/save_settings":
            msg = _save_config(form)
            STATE.add_log(msg)
            self._redirect("/settings")
            return
        self._send_html(html.escape(_t("common.not_found", self._lang())), 404)

    def _redirect_home(self) -> None:
        self._redirect("/")

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    host, port = "127.0.0.1", 8765
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"faceit_ai web UI: {url}")
    print("Press Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    server.serve_forever()


if __name__ == "__main__":
    main()
