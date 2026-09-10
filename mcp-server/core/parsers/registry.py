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

# The third field says whether a `<file>.json` sibling written by the backup
# pipeline is a usable parsed form of this file type.
#
# It is for `.properties`: the pipeline's parse and ours should agree on a flat
# key/value map, which makes the sibling a sound fallback when our parser fails
# and a cross-check when it does not.
#
# It is not for `jvm.config`. That file is a list of JVM flags, and the values
# our parser produces are a normalisation we invented (`-XX:+UseG1GC` becomes
# `-XX:UseG1GC = true`) rather than anything a generic pipeline would emit.
#
# The sibling never carries line numbers, so the raw file stays authoritative
# wherever it parses. Evidence citing a line is what lets a reader check a
# finding without trusting us.
_BY_PATTERN: tuple[tuple[str, Parser, bool], ...] = (
    ("jvm.config", parse_jvm_config, False),
    ("*.properties", parse_properties, True),
    ("*-site.xml", parse_site_xml, False),
)


def parser_for(filename: str) -> Parser | None:
    """Return the parser for a filename, or None if we do not handle it."""
    base = filename.rsplit("/", 1)[-1]
    for pattern, parser, _ in _BY_PATTERN:
        if fnmatch(base, pattern):
            return parser
    return None


def accepts_parsed_sibling(filename: str) -> bool:
    """Whether a `<file>.json` sibling is a usable parsed form of this file."""
    base = filename.rsplit("/", 1)[-1]
    for pattern, _, accepts in _BY_PATTERN:
        if fnmatch(base, pattern):
            return accepts
    return False


def parse(path: str, text: str) -> ParsedFile:
    """Parse a config file, dispatching on its name."""
    parser = parser_for(path)
    if parser is None:
        raise ConfigParseError(f"No parser registered for {path!r}.")
    return parser(path, text)


def is_parseable(filename: str) -> bool:
    """Whether this filename is one we know how to read."""
    return parser_for(filename) is not None
