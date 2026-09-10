"""Config file parsers. Pure functions from text to key/value pairs."""

from core.parsers.base import ConfigParseError, ParsedFile
from core.parsers.jvm_config import HEAP_MAX_FLAG, HEAP_MIN_FLAG, parse_jvm_config
from core.parsers.properties import parse_properties
from core.parsers.registry import (
    accepts_parsed_sibling,
    is_parseable,
    parse,
    parser_for,
)
from core.parsers.xml_site import parse_site_xml

__all__ = [
    "HEAP_MAX_FLAG",
    "accepts_parsed_sibling",
    "HEAP_MIN_FLAG",
    "ConfigParseError",
    "ParsedFile",
    "is_parseable",
    "parse",
    "parse_jvm_config",
    "parse_properties",
    "parse_site_xml",
    "parser_for",
]
