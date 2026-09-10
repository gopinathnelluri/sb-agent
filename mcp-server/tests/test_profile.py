"""Tests for the audit-schema profile.

The profile is the seam that absorbs a Starburst upgrade, so the properties
worth pinning are: a mistyped mapping fails loudly, an unmapped column
degrades to None rather than to a wrong value, and a row maps the same way
every time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.analysis.models import QueryState
from core.analysis.profile import (
    MAPPABLE_FIELDS,
    AuditProfile,
    ProfileError,
    TableLocation,
    default_profile_path,
    load_profile,
)

MINIMAL = """
name: test
table:
  catalog: audit
  schema: public
  name: queries
columns:
  query_id: qid
  elapsed_ms: wall_ms
  queued_ms: queue_ms
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "p.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _profile(tmp_path: Path, body: str = MINIMAL) -> AuditProfile:
    return load_profile(_write(tmp_path, body))


class TestValidation:
    def test_loads_a_valid_profile(self, tmp_path: Path) -> None:
        assert _profile(tmp_path).table.qualified == "audit.public.queries"

    def test_rejects_a_mistyped_field_name(self, tmp_path: Path) -> None:
        """A typo must fail loudly -- silently unmapped looks like a thin source."""
        body = MINIMAL.replace("elapsed_ms: wall_ms", "elapsed_msec: wall_ms")
        with pytest.raises(ProfileError, match="unknown column mapping"):
            _profile(tmp_path, body)

    def test_rejects_unknown_top_level_field(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="unknown field"):
            _profile(tmp_path, MINIMAL + "\nkolumns: {}\n")

    def test_requires_query_id(self, tmp_path: Path) -> None:
        body = MINIMAL.replace("  query_id: qid\n", "")
        with pytest.raises(ProfileError, match="query_id"):
            _profile(tmp_path, body)

    def test_requires_a_table(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="table"):
            _profile(tmp_path, "name: t\ncolumns:\n  query_id: qid\n")

    def test_rejects_invalid_yaml(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="not valid YAML"):
            _profile(tmp_path, "name: [unclosed\n")

    def test_null_mapping_is_deliberate_not_an_error(self, tmp_path: Path) -> None:
        """Explicit null means 'this source does not carry it'."""
        profile = _profile(tmp_path, MINIMAL + "  spilled_bytes: null\n")
        assert "spilled_bytes" not in profile.mapped_fields()

    def test_reports_which_fields_it_cannot_provide(self, tmp_path: Path) -> None:
        unmapped = _profile(tmp_path).unmapped_fields()
        assert "spilled_bytes" in unmapped
        assert "elapsed_ms" not in unmapped


class TestEnvExpansion:
    def test_expands_environment_references(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_CATALOG", "prod_audit")
        body = MINIMAL.replace("catalog: audit", 'catalog: "${MY_CATALOG}"')
        assert _profile(tmp_path, body).table.catalog == "prod_audit"

    def test_missing_variable_fails_at_load(self, tmp_path: Path) -> None:
        body = MINIMAL.replace("catalog: audit", 'catalog: "${NOT_SET_ANYWHERE}"')
        with pytest.raises(ProfileError, match="NOT_SET_ANYWHERE"):
            _profile(tmp_path, body)


class TestRowMapping:
    def _profile_with_states(self, tmp_path: Path) -> AuditProfile:
        return _profile(
            tmp_path,
            MINIMAL
            + "  state: status\n"
            + "state_values:\n  finished: [FINISHED, DONE]\n  failed: [FAILED]\n",
        )

    def test_maps_a_row(self, tmp_path: Path) -> None:
        query = _profile(tmp_path).to_query_info(
            {"qid": "q1", "wall_ms": 5000, "queue_ms": 100}, "prod", "audit:test"
        )
        assert query.query_id == "q1"
        assert query.elapsed_ms == 5000
        assert query.cluster == "prod"

    def test_absent_column_becomes_none_not_zero(self, tmp_path: Path) -> None:
        """The distinction the whole design rests on."""
        query = _profile(tmp_path).to_query_info({"qid": "q1"}, "prod", "s")
        assert query.elapsed_ms is None
        assert query.spilled_bytes is None

    def test_null_value_becomes_none(self, tmp_path: Path) -> None:
        query = _profile(tmp_path).to_query_info(
            {"qid": "q1", "wall_ms": None}, "prod", "s"
        )
        assert query.elapsed_ms is None

    def test_unparseable_number_becomes_none(self, tmp_path: Path) -> None:
        query = _profile(tmp_path).to_query_info(
            {"qid": "q1", "wall_ms": "not a number"}, "prod", "s"
        )
        assert query.elapsed_ms is None

    def test_normalises_state_values(self, tmp_path: Path) -> None:
        profile = self._profile_with_states(tmp_path)
        assert (
            profile.to_query_info({"qid": "q", "status": "DONE"}, "p", "s").state
            is QueryState.FINISHED
        )

    def test_unknown_state_is_unknown_not_a_guess(self, tmp_path: Path) -> None:
        profile = self._profile_with_states(tmp_path)
        assert (
            profile.to_query_info({"qid": "q", "status": "WEIRD"}, "p", "s").state
            is QueryState.UNKNOWN
        )

    def test_select_list_is_deduplicated_and_stable(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        assert profile.select_list() == sorted(set(profile.select_list()))


class TestShippedProfile:
    def test_default_profile_loads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUDIT_CATALOG", "audit")
        monkeypatch.setenv("AUDIT_SCHEMA", "public")
        profile = load_profile(default_profile_path())
        assert profile.name == "sep_event_logger"
        assert "query_id" in profile.mapped_fields()

    def test_every_mapped_field_is_a_real_query_info_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AUDIT_CATALOG", "audit")
        monkeypatch.setenv("AUDIT_SCHEMA", "public")
        assert load_profile(default_profile_path()).mapped_fields() <= MAPPABLE_FIELDS

    def test_table_location_renders_qualified_name(self) -> None:
        assert TableLocation("a", "b", "c").qualified == "a.b.c"


class TestRealSchemaShape:
    """The shipped profile must match the fleet's actual column list.

    These pin the corrections made after seeing a real completed_queries
    table, so a future edit cannot quietly reintroduce a guessed column name.
    """

    def _profile(self, monkeypatch: pytest.MonkeyPatch) -> AuditProfile:
        monkeypatch.setenv("AUDIT_CATALOG", "audit")
        monkeypatch.setenv("AUDIT_SCHEMA", "public")
        return load_profile(default_profile_path())

    def test_user_column_is_usr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`user` is reserved, so the column is spelled `usr`."""
        assert self._profile(monkeypatch).columns["user"] == "usr"

    def test_peak_memory_is_the_user_memory_column(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Query limits are enforced against user memory, not total."""
        profile = self._profile(monkeypatch)
        assert profile.columns["peak_memory_bytes"] == "peak_user_memory_bytes"

    def test_output_rows_is_unmapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """This table records output volume in bytes only."""
        profile = self._profile(monkeypatch)
        assert "output_rows" not in profile.mapped_fields()
        assert profile.columns["output_bytes"] == "output_bytes"

    def test_ambiguous_duration_columns_are_left_unmapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A duration read as milliseconds would be wrong by 1000x.

        `planning_time` and `execution_time` carry no `_ms` suffix, so their
        units are unconfirmed. Absent is safer than wrong.
        """
        profile = self._profile(monkeypatch)
        assert "planning_ms" not in profile.mapped_fields()
        assert "execution_ms" not in profile.mapped_fields()

    def test_operator_payload_is_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is what makes the operator-level detectors buildable at all."""
        profile = self._profile(monkeypatch)
        assert profile.has_operator_detail is True
        assert profile.payload_column == "operator_summaries"

    def test_extra_payloads_are_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        profile = self._profile(monkeypatch)
        assert "cpu_time_distribution" in profile.extra_payloads.values()
        assert "cpu_time_distribution" in profile.select_list()

    def test_failure_info_unpacks_into_code_and_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One structured column, two fields -- the profile absorbs the shape."""
        profile = self._profile(monkeypatch)
        query = profile.to_query_info(
            {
                "query_id": "q1",
                "failure_info": '{"errorCode": {"name": "EXCEEDED_MEMORY_LIMIT"},'
                ' "message": "Query exceeded per-node limit"}',
            },
            "prod",
            "audit",
        )
        assert query.error_code == "EXCEEDED_MEMORY_LIMIT"
        assert query.error_message == "Query exceeded per-node limit"

    def test_plain_text_failure_becomes_the_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = self._profile(monkeypatch)
        query = profile.to_query_info(
            {"query_id": "q1", "failure_info": "connection reset"}, "prod", "audit"
        )
        assert query.error_message == "connection reset"
        assert query.error_code is None
