"""Parser for Hadoop-style site XML (``hive-site.xml``, ``core-site.xml``).

Flattens ``<property><name>..</name><value>..</value></property>`` into the
same key/value shape as a properties file, so rules do not care which format
a setting happens to live in.

These files come from our own backup pipeline, but they are still untrusted
input: a config file is data, and entity-expansion attacks are cheap to
write. Parsing is therefore size-capped and entity resolution is disabled.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from typing import Final

from core.parsers.base import ConfigParseError, ParsedFile

MAX_BYTES: Final = 8 * 1024 * 1024


def parse_site_xml(path: str, text: str) -> ParsedFile:
    """Parse site XML into keys, values and approximate line numbers."""
    if len(text.encode("utf-8", errors="ignore")) > MAX_BYTES:
        raise ConfigParseError(
            f"{path} exceeds the {MAX_BYTES} byte parse limit; refusing to parse."
        )

    _reject_doctype(path, text)
    try:
        root = ElementTree.fromstring(text)  # noqa: S314 - DTDs rejected above
    except ElementTree.ParseError as exc:
        raise ConfigParseError(f"{path} is not well-formed XML: {exc}") from exc

    values: dict[str, str] = {}
    for element in root.iter("property"):
        name = element.findtext("name")
        if not name:
            continue
        values[name.strip()] = (element.findtext("value") or "").strip()

    return ParsedFile(path=path, values=values, lines=_locate(text, values), raw=text)


def _reject_doctype(path: str, text: str) -> None:
    """Refuse a document that declares a DTD or custom entities.

    ElementTree never resolves external entities, so the remaining risk is
    internal entity expansion -- the billion-laughs shape. Both arrive through
    a DOCTYPE declaration, so rejecting that closes the hole without needing a
    third-party parser.
    """
    head = text[:4096].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        raise ConfigParseError(f"{path} declares a DTD or entities; refusing to parse.")


def _locate(text: str, values: dict[str, str]) -> dict[str, int]:
    """Find the line each property name appears on.

    XML has no line-per-entry structure, so this scans for the ``<name>``
    element. Approximate by design -- it points a reader at the right place,
    which is what evidence is for.
    """
    lines: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        for key in values:
            if key not in lines and f"<name>{key}</name>" in line.replace(" ", ""):
                lines[key] = number
    return lines
