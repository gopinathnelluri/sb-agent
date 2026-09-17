"""Connection routing for the audit adapter.

Every cluster keeps its own audit catalog, so a cluster name selects a
connection rather than filtering rows. That makes routing a correctness
concern: connecting to the wrong cluster does not fail, it answers about
somebody else's query.

No network here. A recording stand-in stands in for `trino.dbapi.connect`,
which is enough to pin which host was chosen and how often.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from adapters.trino_audit import (
    TrinoAuditRepository,
    TrinoConnectionError,
    read_endpoints,
)
from core.analysis.profile import AuditProfile, TableLocation

PROFILE = AuditProfile(
    name="test",
    table=TableLocation(catalog="audit", schema="public", name="queries"),
    columns={"query_id": "qid", "elapsed_ms": "wall_ms", "ended_at": "ended"},
)


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.description = [("qid",), ("wall_ms",)]
        self.executed: list[str] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self, host: str = "") -> None:
        self.host = host

    def cursor(self) -> FakeCursor:
        return FakeCursor([])


def _repo(**kwargs: Any) -> TrinoAuditRepository:
    return TrinoAuditRepository(PROFILE, user="fid", password="secret", **kwargs)


class TestEndpointResolution:
    def test_each_cluster_resolves_to_its_own_host(self) -> None:
        repo = _repo(endpoints={"alpha": "alpha.corp.com", "beta": "beta.corp.com"})
        assert repo._endpoint("alpha")[0] == "alpha.corp.com"
        assert repo._endpoint("beta")[0] == "beta.corp.com"

    def test_a_port_may_ride_along_with_the_host(self) -> None:
        repo = _repo(endpoints={"alpha": "alpha.corp.com:8443"})
        assert repo._endpoint("alpha") == ("alpha.corp.com", 8443)

    def test_the_default_port_applies_when_none_is_given(self) -> None:
        repo = _repo(endpoints={"alpha": "alpha.corp.com"})
        assert repo._endpoint("alpha")[1] == 443

    def test_a_single_host_serves_every_cluster(self) -> None:
        """What a one-cluster deployment, or a test, configures."""
        repo = _repo(host="only.corp.com")
        assert repo._endpoint("anything")[0] == "only.corp.com"

    def test_an_unknown_cluster_says_what_is_configured(self) -> None:
        """Better than a timeout: the likely cause is a missing endpoint."""
        repo = _repo(endpoints={"alpha": "alpha.corp.com"})
        with pytest.raises(TrinoConnectionError) as exc:
            repo._endpoint("ghost")
        assert "ghost" in str(exc.value)
        assert "alpha" in str(exc.value)

    def test_configuring_nothing_fails_at_construction(self) -> None:
        with pytest.raises(TrinoConnectionError):
            TrinoAuditRepository(PROFILE, endpoints={}, host="", user="fid")


class TestConnectionsArePerCluster:
    def _repo_with_recorder(self) -> tuple[TrinoAuditRepository, list[str]]:
        opened: list[str] = []
        repo = _repo(endpoints={"alpha": "alpha.corp.com", "beta": "beta.corp.com"})

        def fake_connect(cluster: str) -> Any:
            if cluster not in repo._connections:
                host, _ = repo._endpoint(cluster)
                opened.append(host)
                repo._connections[cluster] = FakeConnection(host)
            return repo._connections[cluster]

        repo._connect = fake_connect  # type: ignore[method-assign]
        return repo, opened

    def test_a_second_call_reuses_the_open_connection(self) -> None:
        repo, opened = self._repo_with_recorder()
        repo.get_query("alpha", "q1")
        repo.get_query("alpha", "q2")
        assert opened == ["alpha.corp.com"]

    def test_another_cluster_gets_its_own(self) -> None:
        repo, opened = self._repo_with_recorder()
        repo.get_query("alpha", "q1")
        repo.get_query("beta", "q2")
        assert opened == ["alpha.corp.com", "beta.corp.com"]
        assert repo._connections["alpha"] is not repo._connections["beta"]

    def test_retention_asks_the_cluster_in_question(self) -> None:
        repo, opened = self._repo_with_recorder()
        repo.retention_days("beta")
        assert opened == ["beta.corp.com"]


class TestNoClusterFilterByDefault:
    """A per-cluster catalog holds only its own queries.

    Filtering on top of that is at best redundant and at worst wrong: the
    shipped profile's tempting candidate, `environment`, holds
    node.environment rather than the cluster name, so a filter would match
    nothing and report every query as missing.
    """

    def test_the_shipped_profile_sets_no_cluster_column(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.analysis.profile import default_profile_path, load_profile

        monkeypatch.setenv("AUDIT_CATALOG", "audit")
        monkeypatch.setenv("AUDIT_SCHEMA", "public")
        assert load_profile(default_profile_path()).cluster_column is None

    def test_the_query_carries_no_cluster_predicate(self) -> None:
        captured: list[str] = []

        class Recording(FakeConnection):
            def cursor(self) -> FakeCursor:
                cursor = FakeCursor([])
                original = cursor.execute

                def execute(sql: str, params: Any = None) -> None:
                    captured.append(sql)
                    original(sql, params)

                cursor.execute = execute  # type: ignore[method-assign]
                return cursor

        repo = _repo(host="one.corp.com", connection=Recording())
        repo.get_query("alpha", "q1")
        assert captured
        assert "environment" not in captured[0]


class TestEndpointsFromTheEnvironment:
    def test_json_in_an_environment_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "TRINO_CLUSTER_ENDPOINTS", json.dumps({"alpha": "alpha.corp.com"})
        )
        assert read_endpoints() == {"alpha": "alpha.corp.com"}

    def test_a_mounted_file_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """So the mapping can be a ConfigMap, changed without redeploying."""
        path = tmp_path / "endpoints.json"
        path.write_text(json.dumps({"beta": "beta.corp.com"}), encoding="utf-8")
        monkeypatch.setenv("TRINO_CLUSTER_ENDPOINTS_FILE", str(path))
        monkeypatch.setenv(
            "TRINO_CLUSTER_ENDPOINTS", json.dumps({"alpha": "alpha.corp.com"})
        )
        assert read_endpoints() == {"beta": "beta.corp.com"}

    def test_malformed_content_does_not_take_the_server_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The config tools never touch a cluster; they must still work."""
        monkeypatch.setenv("TRINO_CLUSTER_ENDPOINTS", "{not json")
        assert read_endpoints() == {}

    def test_nothing_configured_is_an_empty_mapping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRINO_CLUSTER_ENDPOINTS", raising=False)
        monkeypatch.delenv("TRINO_CLUSTER_ENDPOINTS_FILE", raising=False)
        assert read_endpoints() == {}
