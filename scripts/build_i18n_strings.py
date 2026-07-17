#!/usr/bin/env python3
"""Build src/faceit_ai/i18n/strings.json from docs/i18n-string-catalog.md."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "docs" / "i18n-string-catalog.md"
OUT = ROOT / "src" / "faceit_ai" / "i18n" / "strings.json"

_ROW = re.compile(
    r"^\|\s*(?P<key>[a-z][a-z0-9_.]*)\s*\|\s*(?P<en>.*?)\s*\|\s*(?P<de>.*?)\s*\|$"
)


def _norm(s: str) -> str:
    s = s.replace(r"\|", "|")
    s = s.replace("{Review|Blocked}", "{kind}")
    s = s.replace("{review|blocked}", "{kind}")
    s = s.replace("{allowed|blocked}", "{status}")
    return s


def parse_catalog(text: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        m = _ROW.match(line.strip())
        if not m:
            continue
        key = m.group("key")
        if key == "key":
            continue
        en = _norm(m.group("en"))
        de = _norm(m.group("de"))
        if not en:
            continue
        out[key] = {"en": en, "de": de or en}
    return out


def main() -> None:
    text = CATALOG.read_text(encoding="utf-8")
    strings = parse_catalog(text)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(strings, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    missing_de = sum(1 for v in strings.values() if not v.get("de") or v["de"] == v["en"])
    print(f"Wrote {len(strings)} keys to {OUT.relative_to(ROOT)}")
    print(f"Keys where de empty or same as en: {missing_de}")


if __name__ == "__main__":
    main()
