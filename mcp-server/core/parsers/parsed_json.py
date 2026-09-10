"""Reading the ``<file>.json`` form the backup pipeline writes.

Used as a fallback when the raw file cannot be parsed, and as a cross-check
when it can. The raw file stays authoritative wherever it parses, because
only it carries line numbers -- and a finding that cannot cite a line is a
finding the reader has to take on trust.
"""

from __future__ import annotations

import json
from typing import Any

from core.parsers.base import ConfigParseError, ParsedFile


def parse_parsed_json(path: str, text: str) -> ParsedFile:
    """Read a pipeline-parsed config into the same shape as a raw parse.

    Line numbers are absent by construction: the JSON form has none to give.
    """
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigParseError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigParseError(f"{path} does not contain a JSON object.")

    return ParsedFile(
        path=path,
        values={str(k): "" if v is None else str(v) for k, v in raw.items()},
        lines={},
        raw=text,
    )


def disagreements(raw_parse: ParsedFile, json_parse: ParsedFile) -> list[str]:
    """Keys where our parse and the pipeline's differ.

    A disagreement means one of the two is wrong about what the cluster is
    configured with, which is worth surfacing rather than silently preferring
    either side.
    """
    notes: list[str] = []
    for key in sorted(set(raw_parse.values) | set(json_parse.values)):
        ours = raw_parse.values.get(key)
        theirs = json_parse.values.get(key)
        if ours == theirs:
            continue
        if ours is None:
            notes.append(f"{key}: only in the pipeline's parse ({theirs!r})")
        elif theirs is None:
            notes.append(f"{key}: only in our parse ({ours!r})")
        else:
            notes.append(f"{key}: we read {ours!r}, the pipeline read {theirs!r}")
    return notes
