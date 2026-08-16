"""Parser for ``.properties`` files.

Covers ``config.properties``, ``node.properties`` and catalog properties.
Follows the java.util.Properties rules that actually occur in Trino configs:
``#``/``!`` comments, ``=`` or ``:`` separators, and backslash line
continuations. A continued entry reports the line where it started, which is
the line a human would look at.
"""

from __future__ import annotations

from core.parsers.base import ParsedFile

_COMMENT_PREFIXES = ("#", "!")
_SEPARATORS = ("=", ":")


def parse_properties(path: str, text: str) -> ParsedFile:
    """Parse properties text into keys, values and line numbers."""
    values: dict[str, str] = {}
    lines: dict[str, int] = {}

    for start_line, logical in _logical_lines(text):
        stripped = logical.strip()
        if not stripped or stripped.startswith(_COMMENT_PREFIXES):
            continue
        key, value = _split_entry(stripped)
        if not key:
            continue
        values[key] = value
        lines[key] = start_line

    return ParsedFile(path=path, values=values, lines=lines, raw=text)


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash-continued physical lines, keeping the starting line."""
    joined: list[tuple[int, str]] = []
    buffer: list[str] = []
    start = 0

    for number, physical in enumerate(text.splitlines(), start=1):
        if not buffer:
            start = number
        if physical.rstrip().endswith("\\") and not physical.strip().startswith(
            _COMMENT_PREFIXES
        ):
            buffer.append(physical.rstrip()[:-1])
            continue
        buffer.append(physical)
        joined.append((start, "".join(buffer)))
        buffer = []

    if buffer:
        joined.append((start, "".join(buffer)))
    return joined


def _split_entry(entry: str) -> tuple[str, str]:
    """Split on the first unescaped separator."""
    index = _first_separator(entry)
    if index is None:
        return entry.strip(), ""
    key = entry[:index].strip()
    value = entry[index + 1 :].strip()
    return key, value


def _first_separator(entry: str) -> int | None:
    escaped = False
    for index, char in enumerate(entry):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in _SEPARATORS:
            return index
    return None
