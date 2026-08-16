"""Shared fixtures.

The backup fixtures under ``tests/fixtures/backups`` are the substitute for a
real cluster: three clusters covering a clean fleet, a drifted one, and one
missing the properties that rules require.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from adapters.local_backup import LocalBackupRepository
from core.rules.schema import RuleCatalog
from core.scopes import Scope
from core.service import ConfigValidationService

FIXTURE_BACKUPS = Path(__file__).parent / "fixtures" / "backups"
ALL_SCOPES = list(Scope)

# Fixed so that backup_age_days is stable; the fixtures are dated 2026-08-12/13.
FROZEN_NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def catalog() -> RuleCatalog:
    return RuleCatalog.load()


@pytest.fixture
def repository() -> LocalBackupRepository:
    return LocalBackupRepository(FIXTURE_BACKUPS)


@pytest.fixture
def service(
    repository: LocalBackupRepository, catalog: RuleCatalog
) -> ConfigValidationService:
    return ConfigValidationService(repository, catalog, clock=lambda: FROZEN_NOW)


@pytest.fixture
def service_without_manifests(
    tmp_path: Path, catalog: RuleCatalog
) -> ConfigValidationService:
    """A copy of the fixtures with manifests removed, so no version is detectable."""
    root = tmp_path / "backups"
    shutil.copytree(FIXTURE_BACKUPS, root)
    for manifest in root.glob("*/manifest.json"):
        manifest.unlink()
    return ConfigValidationService(
        LocalBackupRepository(root), catalog, clock=lambda: FROZEN_NOW
    )


@pytest.fixture
def stale_backup(tmp_path: Path) -> Path:
    """A backup old enough to trigger the staleness blind spot."""
    root = tmp_path / "backups"
    shutil.copytree(FIXTURE_BACKUPS, root)
    (root / "clean-cluster" / "manifest.json").write_text(
        '{"sep_version": "429-e", "backed_up_at": "2026-01-01T02:00:00Z"}',
        encoding="utf-8",
    )
    return root
