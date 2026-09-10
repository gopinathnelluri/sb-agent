"""Tests for reading the real backup layout.

Three properties matter here. An unrecognised role must be reported rather
than silently dropped, because a whole role vanishing from an audit looks
exactly like a role with nothing wrong. Metadata must be read for files whose
contents were never backed up, since that is the only way to audit a
credential. And describing a backup must not read one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.local_backup import LocalBackupRepository
from core.config.layout import FileKind, classify_all, split_role_host
from core.config.metadata import parse_metadata
from core.models import Role
from core.parsers import accepts_parsed_sibling, is_parseable
from core.parsers.parsed_json import disagreements, parse_parsed_json
from core.parsers.properties import parse_properties
from core.rules.file_checks import build_file_check


@pytest.fixture
def backup(tmp_path: Path) -> Path:
    """A backup in the shape the pipeline actually writes."""
    root = tmp_path / "configs"

    def write(
        role: str, host: str, subdir: str, name: str, body: str, perms: str
    ) -> None:
        target = root / "prod-01" / role / host / subdir
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(body, encoding="utf-8")
        (target / f"{name}.metadata.json").write_text(
            json.dumps(
                {"owner": "starburst", "group": "starburst", "permissions": perms}
            ),
            encoding="utf-8",
        )

    write(
        "coordinator", "c1.corp", "etc/starburst", "config.properties", "a=1\n", "0644"
    )
    write("worker", "w1.corp", "etc/starburst", "config.properties", "a=1\n", "0640")
    # A metastore under its own path entirely.
    write(
        "hms",
        "h1.corp",
        "opt/sbhms/conf",
        "hive-site.xml",
        "<configuration/>\n",
        "0640",
    )
    # A credential whose contents were never backed up -- metadata only.
    keytab = root / "prod-01" / "hms" / "h1.corp" / "opt/sbhms/conf"
    (keytab / "hive.keytab.metadata.json").write_text(
        '{"owner": "hive", "permissions": "0644"}', encoding="utf-8"
    )
    # A role this build does not know about.
    unknown = root / "prod-01" / "mystery-service" / "m1.corp" / "etc/mystery"
    unknown.mkdir(parents=True, exist_ok=True)
    (unknown / "app.properties").write_text("x=1\n", encoding="utf-8")

    (root / "prod-01" / "manifest.json").write_text(
        '{"sep_version": "429-e", "backed_up_at": "2026-09-10T02:00:00Z"}',
        encoding="utf-8",
    )
    return root


class TestPathSplitting:
    def test_splits_role_host_and_opaque_remainder(self) -> None:
        assert split_role_host("worker/w1.corp/etc/starburst/config.properties") == (
            "worker",
            "w1.corp",
            "etc/starburst/config.properties",
        )

    def test_handles_a_deployment_with_a_different_config_path(self) -> None:
        """Nothing may assume etc/starburst -- a metastore lives elsewhere."""
        assert split_role_host("hms/h1/opt/sbhms/conf/hive-site.xml") == (
            "hms",
            "h1",
            "opt/sbhms/conf/hive-site.xml",
        )

    def test_returns_none_for_a_cluster_level_object(self) -> None:
        assert split_role_host("manifest.json") is None


class TestClassification:
    def test_recognises_the_three_forms_of_one_config(self) -> None:
        paths = [
            "etc/starburst/config.properties",
            "etc/starburst/config.properties.json",
            "etc/starburst/config.properties.metadata.json",
        ]
        kinds = {c.kind: c.base for c in classify_all(paths, is_parseable)}
        assert kinds[FileKind.CONFIG] == "etc/starburst/config.properties"
        assert kinds[FileKind.PARSED_CONFIG] == "etc/starburst/config.properties"
        assert kinds[FileKind.METADATA] == "etc/starburst/config.properties"

    def test_metadata_without_a_backed_up_file_still_resolves(self) -> None:
        """The only way to audit a keytab is through metadata it left behind."""
        (entry,) = classify_all(["etc/hive.keytab.metadata.json"], is_parseable)
        assert entry.kind is FileKind.METADATA
        assert entry.base == "etc/hive.keytab"

    def test_a_genuine_json_config_is_not_mistaken_for_a_parsed_sibling(self) -> None:
        (entry,) = classify_all(["etc/resource-groups.json"], is_parseable)
        assert entry.kind is not FileKind.PARSED_CONFIG


class TestParsedSibling:
    def test_properties_accept_a_sibling(self) -> None:
        assert accepts_parsed_sibling("config.properties") is True

    def test_jvm_config_does_not(self) -> None:
        """Our flag normalisation is ours; no pipeline would reproduce it."""
        assert accepts_parsed_sibling("jvm.config") is False

    def test_sibling_parses_to_the_same_shape(self) -> None:
        parsed = parse_parsed_json("c.properties", '{"a": "1", "b": "2"}')
        assert parsed.values == {"a": "1", "b": "2"}
        assert parsed.lines == {}

    def test_cross_check_reports_disagreements(self) -> None:
        ours = parse_properties("c.properties", "a=1\nb=2\n")
        theirs = parse_parsed_json("c.properties.json", '{"a": "1", "b": "3"}')
        assert any("b" in note for note in disagreements(ours, theirs))

    def test_agreement_is_silent(self) -> None:
        ours = parse_properties("c.properties", "a=1\n")
        theirs = parse_parsed_json("c.properties.json", '{"a": "1"}')
        assert disagreements(ours, theirs) == []


class TestRepositoryAgainstRealLayout:
    def test_reads_every_role_including_ones_under_other_paths(
        self, backup: Path
    ) -> None:
        repo = LocalBackupRepository(backup)
        roles = {f.role for f in repo.list_files("prod-01")}
        assert Role.HMS in roles
        assert Role.COORDINATOR in roles

    def test_keeps_the_host_path_opaque(self, backup: Path) -> None:
        repo = LocalBackupRepository(backup)
        hms = [f for f in repo.list_files("prod-01") if f.role is Role.HMS]
        assert any(f.path.startswith("opt/sbhms/conf/") for f in hms)

    def test_picks_up_metadata_for_a_file_never_backed_up(self, backup: Path) -> None:
        repo = LocalBackupRepository(backup)
        bases = {
            f.base for f in repo.list_files("prod-01") if f.kind is FileKind.METADATA
        }
        assert "opt/sbhms/conf/hive.keytab" in bases

    def test_unknown_role_is_reported_not_dropped(self, backup: Path) -> None:
        """The failure that let cache-service vanish from an audit."""
        layout = LocalBackupRepository(backup).layout("prod-01")
        assert "mystery-service" in layout.unknown_roles
        assert "mystery-service" not in layout.roles

    def test_layout_discovers_config_paths_per_role(self, backup: Path) -> None:
        layout = LocalBackupRepository(backup).layout("prod-01")
        assert layout.config_paths["hms"] == ["opt/sbhms/conf"]
        assert layout.config_paths["coordinator"] == ["etc/starburst"]

    def test_layout_reads_no_file(self, backup: Path) -> None:
        """It must stay cheap enough to call across hundreds of clusters."""
        repo = LocalBackupRepository(backup)
        reads = 0
        original = repo.read

        def counting(cluster: str, file: object) -> str:
            nonlocal reads
            reads += 1
            return original(cluster, file)  # type: ignore[arg-type]

        repo.read = counting  # type: ignore[method-assign]
        repo.layout("prod-01")
        assert reads == 0


class TestMetadataChecks:
    def test_world_readable_is_caught(self) -> None:
        meta = parse_metadata(
            "c.properties", '{"permissions": "0644"}', content_backed_up=True
        )
        check = build_file_check({"kind": "not_world_readable"})
        assert check.evaluate(meta).passed is False

    def test_group_readable_is_allowed(self) -> None:
        meta = parse_metadata(
            "c.properties", '{"permissions": "0640"}', content_backed_up=True
        )
        check = build_file_check({"kind": "not_world_readable"})
        assert check.evaluate(meta).passed is True

    def test_max_mode_compares_bits_not_numbers(self) -> None:
        check = build_file_check({"kind": "max_mode", "mode": "0600"})
        loose = parse_metadata("k", '{"permissions": "0644"}', content_backed_up=False)
        tight = parse_metadata("k", '{"permissions": "0600"}', content_backed_up=False)
        assert check.evaluate(loose).passed is False
        assert check.evaluate(tight).passed is True

    def test_unrecorded_permissions_cannot_be_judged(self) -> None:
        """Absent permissions must not read as a pass."""
        meta = parse_metadata("c.properties", "{}", content_backed_up=True)
        check = build_file_check({"kind": "not_world_readable"})
        assert check.evaluate(meta).unavailable

    def test_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown file_check kind"):
            build_file_check({"kind": "not_a_real_check"})
