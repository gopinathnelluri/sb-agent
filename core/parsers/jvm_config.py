"""Parser for ``jvm.config``.

One JVM flag per line. Flags are normalised into key/value pairs so that a
rule can name them the same way it names a Trino property:

    -Xmx80G                     ->  -Xmx                    = 80G
    -XX:+UseG1GC                ->  -XX:UseG1GC             = true
    -XX:-OmitStackTraceInFastThrow -> -XX:OmitStackTraceInFastThrow = false
    -XX:G1HeapRegionSize=32M    ->  -XX:G1HeapRegionSize    = 32M
    -Dcom.sun.foo=bar           ->  -Dcom.sun.foo           = bar

``-Xmx`` is the value the memory rules divide against, so getting it right
matters more than anything else in this file.
"""

from __future__ import annotations

from core.parsers.base import ParsedFile

HEAP_MAX_FLAG = "-Xmx"
HEAP_MIN_FLAG = "-Xms"


def parse_jvm_config(path: str, text: str) -> ParsedFile:
    """Parse ``jvm.config`` into normalised flag key/value pairs."""
    values: dict[str, str] = {}
    lines: dict[str, int] = {}

    for number, physical in enumerate(text.splitlines(), start=1):
        flag = physical.strip()
        if not flag or flag.startswith("#"):
            continue
        key, value = _normalise(flag)
        values[key] = value
        lines[key] = number

    return ParsedFile(path=path, values=values, lines=lines, raw=text)


def _normalise(flag: str) -> tuple[str, str]:
    if flag.startswith("-XX:"):
        return _normalise_hotspot(flag)
    if "=" in flag:
        key, _, value = flag.partition("=")
        return key.strip(), value.strip()
    for sized in (HEAP_MAX_FLAG, HEAP_MIN_FLAG, "-Xss", "-Xmn"):
        if flag.startswith(sized) and len(flag) > len(sized):
            return sized, flag[len(sized) :].strip()
    return flag, "true"


def _normalise_hotspot(flag: str) -> tuple[str, str]:
    """Handle the ``-XX:`` family, including its ``+``/``-`` boolean form."""
    body = flag[len("-XX:") :]
    if "=" in body:
        name, _, value = body.partition("=")
        return f"-XX:{name.strip()}", value.strip()
    if body.startswith(("+", "-")):
        enabled = body.startswith("+")
        return f"-XX:{body[1:].strip()}", "true" if enabled else "false"
    return flag, "true"
