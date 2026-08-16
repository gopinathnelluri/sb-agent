"""Domain errors.

The MCP layer catches these and turns them into tool errors with a message
written for a model to act on -- "which clusters exist" is more useful to the
caller than a stack trace.
"""

from __future__ import annotations


class StarburstAgentError(Exception):
    """Base class for every error this service raises deliberately."""


class ClusterNotFoundError(StarburstAgentError):
    """No backup exists for the requested cluster."""

    def __init__(self, cluster: str, available: list[str]) -> None:
        self.cluster = cluster
        self.available = available
        listed = ", ".join(sorted(available)) or "none"
        super().__init__(
            f"No config backup for cluster {cluster!r}. "
            f"Call list_clusters first. Known clusters: {listed}."
        )


class ConfigFileNotFoundError(StarburstAgentError):
    """The cluster exists but the requested file or node does not."""

    def __init__(self, cluster: str, role: str, file: str, node: str | None) -> None:
        self.cluster = cluster
        self.role = role
        self.file = file
        self.node = node
        where = f"{role}/{node}" if node else role
        super().__init__(
            f"No file {file!r} under {where} for cluster {cluster!r}. "
            "Use get_config_summary to see which source files exist."
        )


class UnredactedSecretError(StarburstAgentError):
    """A secret survived upstream redaction and reached us in cleartext.

    Raised instead of returning the file. The caller gets nothing; a
    high-severity finding is recorded separately. We do not trust the Ansible
    pipeline's redaction, and we never include the offending value in this
    message.
    """

    def __init__(self, cluster: str, file: str, keys: list[str]) -> None:
        self.cluster = cluster
        self.file = file
        self.keys = keys
        super().__init__(
            f"Refusing to return {file!r} from cluster {cluster!r}: "
            f"{len(keys)} key(s) appear to hold an unredacted secret "
            f"({', '.join(sorted(keys))}). This is a backup pipeline defect. "
            "Report it; do not retry."
        )


class UnknownScopeError(StarburstAgentError):
    """A scope was requested that this build does not know about."""

    def __init__(self, requested: list[str], known: list[str]) -> None:
        self.requested = requested
        self.known = known
        super().__init__(
            f"Unknown scope(s): {', '.join(requested)}. "
            f"Valid scopes are: {', '.join(sorted(known))}."
        )
