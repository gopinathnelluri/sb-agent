"""Chooses a parser by filename.

One place that knows the mapping, so adding a format means adding a parser
and one entry here -- not touching the snapshot loader or the rules engine.
"""

from __future__ import annotations

from collections.abc import Callable
from fnmatch import fnmatch

from core.parsers.base import ConfigParseError, ParsedFile
from core.parsers.jvm_config import parse_jvm_config
from core.parsers.properties import parse_properties
from core.parsers.xml_site import parse_site_xml

Parser = Callable[[str, str], ParsedFile]

_BY_PATTERN: tuple[tuple[str, Parser], ...] = (
    ("jvm.config", parse_jvm_config),
    ("*.properties", parse_properties),
    ("*-site.xml", parse_site_xml),
)


def parser_for(filename: str) -> Parser | None:
    """Return the parser for a filename, or None if we do not handle it."""
    base = filename.rsplit("/", 1)[-1]
    for pattern, parser in _BY_PATTERN:
        if fnmatch(base, pattern):
            return parser
    return None


def parse(path: str, text: str) -> ParsedFile:
    """Parse a config file, dispatching on its name."""
    parser = parser_for(path)
    if parser is None:
        raise ConfigParseError(f"No parser registered for {path!r}.")
    return parser(path, text)


def is_parseable(filename: str) -> bool:
    """Whether this filename is one we know how to read."""
    return parser_for(filename) is not None
