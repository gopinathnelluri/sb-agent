"""The shape every parser produces.

Line numbers are not a nicety: a finding without a file and line is not
admissible under CLAUDE.md, so a parser that loses them is broken.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ConfigParseError(ValueError):
    """A config file could not be read.

    Never fatal to a run. The file is recorded as a ``ParseFailure`` and the
    rules that needed it are skipped, which shows up in coverage rather than
    being silently absorbed.
    """


@dataclass(frozen=True)
class ParsedFile:
    """One config file, flattened to key/value pairs with source positions."""

    path: str
    values: dict[str, str]
    lines: dict[str, int] = field(default_factory=dict)
    raw: str = ""

    def line_of(self, key: str) -> int | None:
        """Line number where ``key`` was defined, if known."""
        return self.lines.get(key)
