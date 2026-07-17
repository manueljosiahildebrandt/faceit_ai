"""UI language helpers (EN / DE). Strings load from strings.json (built from docs/i18n-string-catalog.md)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

Lang = Literal["en", "de"]
SUPPORTED_LANGS: tuple[Lang, ...] = ("en", "de")
DEFAULT_LANG: Lang = "en"
COOKIE_NAME = "facit_lang"

_STRINGS_PATH = Path(__file__).resolve().parent / "strings.json"


def _load_strings() -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(_STRINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict[str, str]] = {}
    if isinstance(raw, dict):
        for key, val in raw.items():
            if isinstance(val, dict):
                out[str(key)] = {
                    "en": str(val.get("en") or ""),
                    "de": str(val.get("de") or val.get("en") or ""),
                }
    return out


_STRINGS: dict[str, dict[str, str]] = _load_strings()


def normalize_lang(raw: str | None) -> Lang:
    s = (raw or "").strip().lower()
    if s.startswith("de"):
        return "de"
    if s.startswith("en"):
        return "en"
    return DEFAULT_LANG


def lang_from_cookie_header(cookie_header: str | None) -> Lang:
    raw = cookie_header or ""
    for part in raw.split(";"):
        part = part.strip()
        if not part.startswith(f"{COOKIE_NAME}="):
            continue
        return normalize_lang(part.split("=", 1)[1])
    return DEFAULT_LANG


def t(key: str, lang: Lang | str, **kwargs: object) -> str:
    lang_n = normalize_lang(str(lang))
    row = _STRINGS.get(key) or {}
    text = row.get(lang_n) or row.get(DEFAULT_LANG) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, ValueError):
            return text
    return text


def strings_for_lang(lang: Lang | str) -> dict[str, str]:
    """Flat key→text map for embedding into JS."""
    lang_n = normalize_lang(str(lang))
    out: dict[str, str] = {}
    for key, row in _STRINGS.items():
        out[key] = row.get(lang_n) or row.get(DEFAULT_LANG) or key
    return out


def i18n_bootstrap_script(lang: Lang | str) -> str:
    """Script tag: window.FACIT_I18N + window.t(key, vars)."""
    payload = json.dumps(strings_for_lang(lang), ensure_ascii=False)
    return f"""<script>
window.FACIT_I18N = {payload};
window.FACIT_LANG = {json.dumps(normalize_lang(str(lang)))};
window.t = function(key, vars) {{
  var s = (window.FACIT_I18N && window.FACIT_I18N[key]) || key;
  if (vars && typeof vars === 'object') {{
    Object.keys(vars).forEach(function(k) {{
      s = s.split('{{' + k + '}}').join(String(vars[k]));
    }});
  }}
  return s;
}};
</script>"""
