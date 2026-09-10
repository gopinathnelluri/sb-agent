"""Loading an audit-schema profile.

A profile says which columns in *your* audit table correspond to the fields
``QueryInfo`` declares. Keeping that mapping as data rather than code is what
makes a Starburst upgrade -- or onboarding a cluster whose event logger writes
different column names -- a YAML edit instead of a patch.

Validation is strict at load time. An unknown key is an error rather than an
ignored line, for the same reason it is in the rule catalog: a mapping
silently disabled by a typo looks exactly like a source that never carried
the column.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core.analysis.models import QueryInfo, QueryState

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# Every QueryInfo field a profile may map. Anything else is a typo.
MAPPABLE_FIELDS: frozenset[str] = frozenset(
    {
        "query_id",
        "state",
        "sql",
        "user",
        "session_catalog",
        "session_schema",
        "elapsed_ms",
        "queued_ms",
        "planning_ms",
        "execution_ms",
        "cpu_ms",
        "peak_memory_bytes",
        "total_bytes_scanned",
        "total_rows",
        "output_rows",
        "output_bytes",
        "written_rows",
        "written_bytes",
        "spilled_bytes",
        "completed_splits",
        "physical_input_bytes",
        "physical_input_rows",
        "internal_network_bytes",
        "error_code",
        "error_message",
        "error_info",
        "started_at",
        "ended_at",
    }
)

_REQUIRED_FIELDS = frozenset({"query_id"})
_TOP_LEVEL = frozenset(
    {
        "name",
        "description",
        "table",
        "cluster_column",
        "columns",
        "payload",
        "extra_payloads",
        "state_values",
    }
)


class ProfileError(ValueError):
    """A profile is malformed. Raised at load time, never mid-request."""


@dataclass(frozen=True)
class TableLocation:
    """Where the query history lives."""

    catalog: str
    schema: str
    name: str

    @property
    def qualified(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.name}"


@dataclass(frozen=True)
class AuditProfile:
    """A validated mapping from audit columns to ``QueryInfo`` fields."""

    name: str
    table: TableLocation
    columns: dict[str, str]
    cluster_column: str | None = None
    payload_column: str | None = None
    extra_payloads: dict[str, str] = field(default_factory=dict)
    state_values: dict[QueryState, frozenset[str]] = field(default_factory=dict)
    description: str = ""

    # -- projection ------------------------------------------------------

    def select_list(self) -> list[str]:
        """Columns to SELECT, deduplicated and ordered for a stable query."""
        wanted = sorted(set(self.columns.values()))
        if self.cluster_column and self.cluster_column not in wanted:
            wanted.append(self.cluster_column)
        if self.payload_column and self.payload_column not in wanted:
            wanted.append(self.payload_column)
        for column in self.extra_payloads.values():
            if column not in wanted:
                wanted.append(column)
        return wanted

    @property
    def has_operator_detail(self) -> bool:
        """Whether this source can support the operator-level detectors."""
        return self.payload_column is not None

    def mapped_fields(self) -> frozenset[str]:
        """QueryInfo fields this profile can actually populate."""
        return frozenset(self.columns)

    def unmapped_fields(self) -> frozenset[str]:
        """Fields this source cannot provide -- the detectors needing them skip."""
        return MAPPABLE_FIELDS - self.mapped_fields()

    # -- row mapping -----------------------------------------------------

    def to_query_info(
        self, row: dict[str, Any], cluster: str, source: str
    ) -> QueryInfo:
        """Map one audit row into the normalised type.

        A column present in the profile but absent or NULL in the row yields
        ``None`` rather than an error -- the detectors treat that as "not
        available" and say so in coverage.
        """

        def value(field_name: str) -> Any:
            column = self.columns.get(field_name)
            return row.get(column) if column else None

        def as_int(field_name: str) -> int | None:
            raw = value(field_name)
            if raw is None:
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        def as_str(field_name: str) -> str | None:
            raw = value(field_name)
            return None if raw is None else str(raw)

        code, message = self._failure(value("error_info"))
        if code is None:
            code = as_str("error_code")
        if message is None:
            message = as_str("error_message")

        return QueryInfo(
            query_id=str(value("query_id") or ""),
            cluster=cluster,
            source=source,
            state=self._state(as_str("state")),
            sql=as_str("sql"),
            user=as_str("user"),
            session_catalog=as_str("session_catalog"),
            session_schema=as_str("session_schema"),
            elapsed_ms=as_int("elapsed_ms"),
            queued_ms=as_int("queued_ms"),
            planning_ms=as_int("planning_ms"),
            execution_ms=as_int("execution_ms"),
            cpu_ms=as_int("cpu_ms"),
            peak_memory_bytes=as_int("peak_memory_bytes"),
            total_bytes_scanned=as_int("total_bytes_scanned"),
            total_rows=as_int("total_rows"),
            output_rows=as_int("output_rows"),
            output_bytes=as_int("output_bytes"),
            written_rows=as_int("written_rows"),
            written_bytes=as_int("written_bytes"),
            physical_input_bytes=as_int("physical_input_bytes"),
            physical_input_rows=as_int("physical_input_rows"),
            internal_network_bytes=as_int("internal_network_bytes"),
            spilled_bytes=as_int("spilled_bytes"),
            completed_splits=as_int("completed_splits"),
            error_code=code,
            error_message=message,
            started_at=as_str("started_at"),
            ended_at=as_str("ended_at"),
        )

    @staticmethod
    def _failure(raw: Any) -> tuple[str | None, str | None]:
        """Unpack a structured failure column into a code and a message.

        Sources differ: some record two columns, some one JSON blob. Reading
        whichever is present keeps the profile the only place that has to know.
        """
        if raw is None:
            return None, None
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return None, None
            if not stripped.startswith("{"):
                return None, stripped
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError:
                return None, stripped
        if not isinstance(raw, dict):
            return None, str(raw)
        code = raw.get("errorCode") or raw.get("error_code") or raw.get("type")
        message = raw.get("message") or raw.get("failureMessage")
        if isinstance(code, dict):
            code = code.get("name") or code.get("code")
        return (
            None if code is None else str(code),
            None if message is None else str(message),
        )

    def _state(self, raw: str | None) -> QueryState:
        if not raw:
            return QueryState.UNKNOWN
        upper = raw.strip().upper()
        for state, accepted in self.state_values.items():
            if upper in accepted:
                return state
        try:
            return QueryState(raw.strip().lower())
        except ValueError:
            return QueryState.UNKNOWN


def _expand(value: Any) -> Any:
    """Substitute ${ENV_VAR} references. Credentials never live in the file."""
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.environ.get(name)
        if resolved is None:
            raise ProfileError(
                f"Profile references ${{{name}}} but that environment variable "
                f"is not set."
            )
        return resolved

    return _ENV_PATTERN.sub(replace, value)


def load_profile(path: Path) -> AuditProfile:
    """Load and validate one profile."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"{path.name} must contain a mapping.")

    unknown = set(raw) - _TOP_LEVEL
    if unknown:
        raise ProfileError(
            f"{path.name}: unknown field(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(_TOP_LEVEL))}."
        )

    table_raw = raw.get("table")
    if not isinstance(table_raw, dict):
        raise ProfileError(f"{path.name}: 'table' must be a mapping.")
    missing_table = {"catalog", "schema", "name"} - set(table_raw)
    if missing_table:
        raise ProfileError(
            f"{path.name}: table needs {', '.join(sorted(missing_table))}."
        )

    columns_raw = raw.get("columns")
    if not isinstance(columns_raw, dict):
        raise ProfileError(f"{path.name}: 'columns' must be a mapping.")

    unknown_cols = set(columns_raw) - MAPPABLE_FIELDS
    if unknown_cols:
        raise ProfileError(
            f"{path.name}: unknown column mapping(s): "
            f"{', '.join(sorted(unknown_cols))}. "
            f"Mappable fields: {', '.join(sorted(MAPPABLE_FIELDS))}."
        )

    # A null value means "this source does not carry it" -- deliberate, not a typo.
    columns = {k: str(v) for k, v in columns_raw.items() if v is not None}

    missing_required = _REQUIRED_FIELDS - set(columns)
    if missing_required:
        raise ProfileError(
            f"{path.name}: must map {', '.join(sorted(missing_required))}."
        )

    extras_raw = raw.get("extra_payloads") or {}
    if not isinstance(extras_raw, dict):
        raise ProfileError(f"{path.name}: 'extra_payloads' must be a mapping.")
    extras = {str(k): str(v) for k, v in extras_raw.items() if v is not None}

    payload_raw = raw.get("payload") or {}
    if not isinstance(payload_raw, dict):
        raise ProfileError(f"{path.name}: 'payload' must be a mapping.")
    payload_column = payload_raw.get("column")

    states: dict[QueryState, frozenset[str]] = {}
    for key, values in (raw.get("state_values") or {}).items():
        try:
            state = QueryState(str(key).lower())
        except ValueError as exc:
            known = ", ".join(s.value for s in QueryState)
            raise ProfileError(
                f"{path.name}: unknown state '{key}'. Known: {known}."
            ) from exc
        if not isinstance(values, list):
            raise ProfileError(f"{path.name}: state '{key}' must map to a list.")
        states[state] = frozenset(str(v).upper() for v in values)

    return AuditProfile(
        name=str(raw.get("name") or path.stem),
        description=str(raw.get("description") or "").strip(),
        table=TableLocation(
            catalog=str(_expand(table_raw["catalog"])),
            schema=str(_expand(table_raw["schema"])),
            name=str(_expand(table_raw["name"])),
        ),
        columns=columns,
        cluster_column=(
            str(raw["cluster_column"]) if raw.get("cluster_column") else None
        ),
        payload_column=str(payload_column) if payload_column else None,
        extra_payloads=extras,
        state_values=states,
    )


def default_profile_path() -> Path:
    """The profile shipped with the image."""
    return Path(__file__).parent / "profiles" / "sep_event_logger.yaml"
