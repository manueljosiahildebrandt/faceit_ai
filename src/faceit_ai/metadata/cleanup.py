"""Identify and strip tool-owned keywords (``sola/…``, ``sola|…``)."""

from __future__ import annotations


def is_tool_owned_plain(keyword: str) -> bool:
    s = keyword.strip()
    return s.startswith("sola/")


def is_tool_owned_hierarchical(keyword: str) -> bool:
    s = keyword.strip()
    return s.startswith("sola|")


def filter_preserve_plain(existing: list[str], new_tool: tuple[str, ...]) -> list[str]:
    kept = [x for x in existing if not is_tool_owned_plain(x)]
    merged = list(kept) + list(new_tool)
    # stable unique order: preserve kept order, append new_tool in order, dedupe
    seen: set[str] = set()
    out: list[str] = []
    for k in merged:
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def filter_preserve_hierarchical(existing: list[str], new_tool: tuple[str, ...]) -> list[str]:
    kept = [x for x in existing if not is_tool_owned_hierarchical(x)]
    merged = list(kept) + list(new_tool)
    seen: set[str] = set()
    out: list[str] = []
    for k in merged:
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def tool_owned_plain_from_list(items: list[str]) -> list[str]:
    return [x for x in items if is_tool_owned_plain(x)]


def tool_owned_hierarchical_from_list(items: list[str]) -> list[str]:
    return [x for x in items if is_tool_owned_hierarchical(x)]
