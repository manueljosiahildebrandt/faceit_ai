"""Simple desktop GUI for faceit_ai batch operations."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path

import yaml

from faceit_ai.persistence.repository import ConsentRepository
from faceit_ai.persistence.session import create_engine_and_session_factory, session_scope
from faceit_ai.settings import load_settings, parse_raw_decode_size, resolve_config_path


class FaceitGui:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self._tk = tk
        self._filedialog = filedialog
        self._messagebox = messagebox
        self._ttk = ttk
        self.root = tk.Tk()
        self.root.title("faceit_ai batch GUI")
        self.root.geometry("1080x760")

        self.log_queue: queue.Queue[str] = queue.Queue()
        self._running = False

        self.people_root_var = tk.StringVar()
        self.search_folder_var = tk.StringVar()
        self.usage_var = tk.StringVar(value="social")
        self.force_var = tk.BooleanVar(value=False)
        self.quiet_var = tk.BooleanVar(value=False)
        self.sync_metadata_var = tk.BooleanVar(value=False)
        self.status_blocked_var = tk.BooleanVar(value=True)
        self.status_review_var = tk.BooleanVar(value=True)
        self.status_ok_var = tk.BooleanVar(value=False)
        self.export_mode_var = tk.StringVar(value="copy")

        self.det_size_var = tk.StringVar()
        self.max_dimension_var = tk.StringVar()
        self.raw_half_var = tk.BooleanVar(value=True)
        self.meta_enabled_var = tk.BooleanVar(value=True)
        self.verify_after_write_var = tk.BooleanVar(value=False)
        self.config_path = resolve_config_path()
        self.log_text: tk.Text

        self._build_ui()
        self._load_settings_into_form()
        self.root.after(120, self._drain_log_queue)

    def _build_ui(self) -> None:
        tk = self._tk
        ttk = self._ttk
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        run_tab = ttk.Frame(nb)
        tune_tab = ttk.Frame(nb)
        nb.add(run_tab, text="Run Batch")
        nb.add(tune_tab, text="Search/Metadata Settings")

        self._build_run_tab(run_tab)
        self._build_tune_tab(tune_tab)

    def _build_run_tab(self, parent: Any) -> None:
        tk = self._tk
        ttk = self._ttk
        top = ttk.LabelFrame(parent, text="People Folder (Register Missing)")
        top.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(
            top,
            text="Structure: people/<name>/photo1..n (one folder per person).",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(6, 2))
        ttk.Entry(top, textvariable=self.people_root_var).grid(
            row=1, column=0, sticky="ew", padx=8, pady=6
        )
        ttk.Button(top, text="Browse", command=self._pick_people_root).grid(
            row=1, column=1, padx=6, pady=6
        )
        ttk.Button(
            top,
            text="Start Scan People Folder",
            command=self._scan_people_folder,
        ).grid(row=1, column=2, padx=6, pady=6)
        top.columnconfigure(0, weight=1)

        mid = ttk.LabelFrame(parent, text="Analyze + Auto Metadata Sync")
        mid.pack(fill=tk.X, padx=8, pady=8)

        ttk.Label(mid, text="Folder to search:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(mid, textvariable=self.search_folder_var).grid(
            row=0, column=1, sticky="ew", padx=6, pady=6
        )
        ttk.Button(mid, text="Browse", command=self._pick_search_folder).grid(
            row=0, column=2, padx=6, pady=6
        )
        mid.columnconfigure(1, weight=1)

        row2 = ttk.Frame(mid)
        row2.grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=4)
        ttk.Label(row2, text="Usage:").pack(side=tk.LEFT, padx=(2, 4))
        ttk.Combobox(
            row2,
            values=["social", "web", "internal", "print"],
            textvariable=self.usage_var,
            width=10,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Checkbutton(row2, text="Force", variable=self.force_var).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(row2, text="Quiet", variable=self.quiet_var).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(
            row2, text="Auto metadata sync after analyze", variable=self.sync_metadata_var
        ).pack(side=tk.LEFT, padx=6)

        row3 = ttk.Frame(mid)
        row3.grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=(2, 8))
        ttk.Label(row3, text="Export flagged:").pack(side=tk.LEFT, padx=(2, 4))
        ttk.Combobox(
            row3,
            values=["off", "copy", "move"],
            textvariable=self.export_mode_var,
            width=8,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(row3, text="Statuses for flagged/sync:").pack(side=tk.LEFT, padx=(2, 4))
        ttk.Checkbutton(row3, text="blocked", variable=self.status_blocked_var).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Checkbutton(row3, text="review", variable=self.status_review_var).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Checkbutton(row3, text="ok", variable=self.status_ok_var).pack(side=tk.LEFT, padx=4)

        ttk.Button(
            mid,
            text="Start Analyze Batch",
            command=self._run_analyze_batch,
        ).grid(row=3, column=0, columnspan=3, sticky="ew", padx=8, pady=(2, 10))

        logs = ttk.LabelFrame(parent, text="Live Log")
        logs.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.log_text = tk.Text(logs, wrap="word", height=20)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y = ttk.Scrollbar(logs, orient=tk.VERTICAL, command=self.log_text.yview)
        y.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=y.set)

        bottom = ttk.Frame(parent)
        bottom.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(bottom, text="Clear Log", command=self._clear_log).pack(side=tk.LEFT)

    def _build_tune_tab(self, parent: Any) -> None:
        ttk = self._ttk
        frame = ttk.LabelFrame(parent, text="Performance / Metadata Settings")
        frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(frame, text=f"Config file: {self.config_path}").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 10)
        )

        ttk.Label(frame, text="det_size (e.g. 512,512):").grid(
            row=1, column=0, sticky="w", padx=8, pady=5
        )
        ttk.Entry(frame, textvariable=self.det_size_var, width=16).grid(
            row=1, column=1, sticky="w", padx=6, pady=5
        )

        ttk.Label(frame, text="max_dimension:").grid(row=1, column=2, sticky="w", padx=8, pady=5)
        ttk.Entry(frame, textvariable=self.max_dimension_var, width=10).grid(
            row=1, column=3, sticky="w", padx=6, pady=5
        )

        ttk.Checkbutton(frame, text="RAW half-size decode", variable=self.raw_half_var).grid(
            row=2, column=0, sticky="w", padx=8, pady=5
        )
        ttk.Checkbutton(frame, text="Metadata enabled", variable=self.meta_enabled_var).grid(
            row=2, column=1, sticky="w", padx=8, pady=5
        )
        ttk.Checkbutton(
            frame,
            text="ExifTool verify after write",
            variable=self.verify_after_write_var,
        ).grid(row=2, column=2, columnspan=2, sticky="w", padx=8, pady=5)

        btns = ttk.Frame(frame)
        btns.grid(row=3, column=0, columnspan=4, sticky="ew", padx=8, pady=(12, 8))
        ttk.Button(btns, text="Reload from config", command=self._load_settings_into_form).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btns, text="Save settings to YAML", command=self._save_settings_from_form).pack(
            side=tk.LEFT
        )

    def _pick_people_root(self) -> None:
        p = self._filedialog.askdirectory(title="Choose people folder")
        if p:
            self.people_root_var.set(p)

    def _pick_search_folder(self) -> None:
        p = self._filedialog.askdirectory(title="Choose folder to analyze")
        if p:
            self.search_folder_var.set(p)

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)

    def _append_log(self, text: str) -> None:
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)

    def _drain_log_queue(self) -> None:
        while True:
            try:
                chunk = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(chunk)
        self.root.after(120, self._drain_log_queue)

    def _selected_statuses(self) -> list[str]:
        out: list[str] = []
        if self.status_blocked_var.get():
            out.append("blocked")
        if self.status_review_var.get():
            out.append("review")
        if self.status_ok_var.get():
            out.append("ok")
        return out

    def _resolve_cmd(self, cli_name: str) -> str:
        local = Path.cwd() / ".venv" / "bin" / cli_name
        return str(local) if local.is_file() else cli_name

    def _run_worker(self, title: str, commands: list[list[str]]) -> None:
        if self._running:
            self._messagebox.showwarning("Busy", "Another task is already running.")
            return

        self._running = True

        def _thread() -> None:
            try:
                self.log_queue.put(f"\n=== {title} ===\n")
                for cmd in commands:
                    shown = " ".join(cmd)
                    self.log_queue.put(f"\n$ {shown}\n")
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                    )
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        self.log_queue.put(line)
                    rc = proc.wait()
                    if rc != 0:
                        self.log_queue.put(f"\n[ERROR] command failed with exit code {rc}\n")
                        break
                self.log_queue.put("\n=== Done ===\n")
            except Exception as e:  # pragma: no cover
                self.log_queue.put(f"\n[ERROR] {e}\n")
            finally:
                self._running = False

        threading.Thread(target=_thread, daemon=True).start()

    def _scan_people_folder(self) -> None:
        people_root = self.people_root_var.get().strip()
        if not people_root:
            self._messagebox.showerror("Missing folder", "Please choose the people folder first.")
            return
        root = Path(people_root).expanduser()
        if not root.is_dir():
            self._messagebox.showerror("Invalid folder", f"Not a folder: {root}")
            return

        settings = load_settings()
        _, session_factory = create_engine_and_session_factory(settings.database_url)
        existing_names: set[str] = set()
        with session_scope(session_factory) as session:
            repo = ConsentRepository(session)
            for d in sorted(root.iterdir()):
                if d.is_dir() and repo.get_active_person_by_name(d.name) is not None:
                    existing_names.add(d.name)

        register_cmd = self._resolve_cmd("register_person")
        commands: list[list[str]] = []
        added = 0
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            name = d.name.strip()
            if not name or name in existing_names:
                self.log_queue.put(f"[skip] person already exists: {name}\n")
                continue
            commands.append([register_cmd, str(d), name, "--no-consent"])
            added += 1

        if not commands:
            self.log_queue.put("\nNo new people to register.\n")
            return
        self._run_worker(f"Register missing people ({added})", commands)

    def _run_analyze_batch(self) -> None:
        folder = self.search_folder_var.get().strip()
        if not folder:
            self._messagebox.showerror("Missing folder", "Please choose the folder to analyze.")
            return
        statuses = self._selected_statuses()
        if not statuses:
            self._messagebox.showerror("Missing status", "Choose at least one status checkbox.")
            return

        cmd = [
            self._resolve_cmd("analyze_photos"),
            folder,
            "--usage",
            self.usage_var.get(),
            "--export-flagged",
            self.export_mode_var.get(),
        ]
        if self.force_var.get():
            cmd.append("--force")
        if self.quiet_var.get():
            cmd.append("--quiet")
        cmd.extend(["--sync-metadata"] if self.sync_metadata_var.get() else ["--no-sync-metadata"])
        for st in statuses:
            cmd.extend(["--flagged-status", st])
        people_root = self.people_root_var.get().strip()
        if people_root:
            cmd.extend(["--collect-to", people_root])

        self._run_worker("Analyze batch", [cmd])

    def _load_settings_into_form(self) -> None:
        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        pi = (cfg.get("pipeline") or {}).get("insightface") or {}
        img = (cfg.get("pipeline") or {}).get("image") or {}
        md = cfg.get("metadata") or {}

        det = pi.get("det_size", [512, 512])
        if isinstance(det, (list, tuple)) and len(det) == 2:
            self.det_size_var.set(f"{int(det[0])},{int(det[1])}")
        self.max_dimension_var.set(str(int(img.get("max_dimension", 1800))))
        self.raw_half_var.set(parse_raw_decode_size(img) != "full")
        self.meta_enabled_var.set(bool(md.get("enabled", True)))
        self.verify_after_write_var.set(bool(md.get("exiftool_verify_after_write", False)))

    def _save_settings_from_form(self) -> None:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        parts = [p.strip() for p in self.det_size_var.get().split(",")]
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            self._messagebox.showerror("Invalid det_size", "Use format like 512,512")
            return
        if not self.max_dimension_var.get().strip().isdigit():
            self._messagebox.showerror("Invalid max_dimension", "max_dimension must be an integer")
            return

        raw.setdefault("pipeline", {}).setdefault("insightface", {})["det_size"] = [
            int(parts[0]),
            int(parts[1]),
        ]
        raw.setdefault("pipeline", {}).setdefault("image", {})["max_dimension"] = int(
            self.max_dimension_var.get().strip()
        )
        img_cfg = raw.setdefault("pipeline", {}).setdefault("image", {})
        img_cfg["raw_decode_size"] = "half" if bool(self.raw_half_var.get()) else "full"
        img_cfg.pop("raw_half_size", None)
        raw.setdefault("metadata", {})["enabled"] = bool(self.meta_enabled_var.get())
        raw.setdefault("metadata", {})["exiftool_verify_after_write"] = bool(
            self.verify_after_write_var.get()
        )

        self.config_path.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=False),
            encoding="utf-8",
        )
        self._messagebox.showinfo("Saved", f"Updated settings in:\n{self.config_path}")

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    try:
        import tkinter  # noqa: F401
    except ModuleNotFoundError:
        print(
            "faceit_ai_gui requires Tkinter, but this Python has no _tkinter module.\n"
            "Use a Python build with Tk support, then reinstall venv dependencies.\n\n"
            "macOS quick options:\n"
            "  1) Install python.org Python (includes Tk), create a new venv, pip install -e .\n"
            "  2) Or install Tcl/Tk + rebuild Python/venv with Tk support.\n\n"
            "Then run: faceit_ai_gui",
            file=sys.stderr,
        )
        raise SystemExit(2)
    app = FaceitGui()
    app.run()


if __name__ == "__main__":
    main()
