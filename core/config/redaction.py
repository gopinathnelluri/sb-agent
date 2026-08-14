"""Verification that secrets really were redacted upstream.

The Ansible backup pipeline is supposed to redact before writing to COS. We
do not trust it: a pipeline regression would otherwise turn this service into
a credential exfiltration tool with a friendly API.

Checking key names alone is not enough. Secrets hide inside the *values* of
innocuously named keys -- a JDBC URL with ``?password=``, an S3 URI with
embedded credentials, a JVM flag carrying a truststore password. Both are
checked here.

Nothing in this module ever puts a suspected secret value into a message,
a log line or an exception.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Final

SECRET_KEY_PATTERNS: Final[tuple[str, ...]] = (
    "*password*",
    "*passwd*",
    "*secret*",
    "*keytab*",
    "*credential*",
    "*token*",
    "*private-key*",
    "*privatekey*",
    "*access-key*",
    "*accesskey*",
    "*.key",
)

_PLACEHOLDER_EXACT: Final[frozenset[str]] = frozenset(
    {
        "",
        "*",
        "***",
        "****",
        "*****",
        "********",
        "redacted",
        "<redacted>",
        "[redacted]",
        "changeme",
        "xxx",
        "xxxx",
        "none",
        "null",
        "placeholder",
    }
)

# Templated or externalised values were never secrets in the first place.
_PLACEHOLDER_SHAPES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^\$\{[^}]*\}$"),  # ${ENV_VAR}
    re.compile(r"^\{\{[^}]*\}\}$"),  # {{ ansible_var }}
    re.compile(r"^<[^>]+>$"),  # <fill-me-in>
    re.compile(r"^\*+$"),  # ******
    re.compile(r"^!+[A-Z_]+!+$"),  # !!REDACTED!!
)

# Credentials embedded inside an otherwise ordinary-looking value.
_EMBEDDED: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(r"[?&;](?:password|pwd|passwd)=(?![\s&;]*$)[^&;\s]+", re.I),
        "connection string carries an inline password parameter",
    ),
    (
        re.compile(r"://[^/\s:@]+:[^/\s:@]+@"),
        "URI carries inline user:password credentials",
    ),
    (
        re.compile(r"\b(?:aws_)?secret_access_key\s*=\s*\S+", re.I),
        "value embeds an access key assignment",
    ),
    (
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "value embeds a PEM private key",
    ),
)


@dataclass(frozen=True)
class SecretHit:
    """A key whose value appears to be an unredacted secret.

    Carries the key and a reason, never the value.
    """

    key: str
    reason: str


def is_placeholder(value: str) -> bool:
    """Whether a value looks like a redaction marker rather than a real secret."""
    stripped = value.strip()
    if stripped.lower() in _PLACEHOLDER_EXACT:
        return True
    return any(shape.match(stripped) for shape in _PLACEHOLDER_SHAPES)


def is_secret_key(key: str) -> bool:
    """Whether a key name implies its value is a credential."""
    lowered = key.lower()
    return any(fnmatch(lowered, pattern) for pattern in SECRET_KEY_PATTERNS)


def find_unredacted(values: dict[str, str]) -> list[SecretHit]:
    """Return keys that appear to hold a live secret.

    An empty list means the file passed verification.
    """
    hits: list[SecretHit] = []
    for key, value in sorted(values.items()):
        if is_secret_key(key) and not is_placeholder(value):
            hits.append(
                SecretHit(
                    key=key,
                    reason="key names a credential and value is not a placeholder",
                )
            )
            continue
        for pattern, reason in _EMBEDDED:
            if pattern.search(value):
                hits.append(SecretHit(key=key, reason=reason))
                break
    return hits


def redact_text(text: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """Blank out any value belonging to a secret-named key.

    Applied to file content on the way out, so that a file which passed
    verification still does not leak a credential we failed to classify.
    Returns the redacted text and the keys that were masked.
    """
    redacted = text
    masked: list[str] = []
    for key, value in sorted(values.items()):
        if not value or not is_secret_key(key) or is_placeholder(value):
            continue
        redacted = redacted.replace(value, "***REDACTED***")
        masked.append(key)
    return redacted, masked
