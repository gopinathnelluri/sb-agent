"""Process entrypoint.

Wiring only: pick a repository, build the service, serve. This is the one
place that reads the environment, so every other module stays importable
without configuration -- which is what keeps the tests offline and the
container's startup free of surprises under a random UID.

    python -m mcp_server --backend local --root /mnt/backups
    python -m mcp_server --backend cos          # bucket from COS_BUCKET
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from core.analysis.profile import ProfileError, default_profile_path, load_profile
from core.analysis.service import QueryAnalysisService
from core.ports import ConfigRepository
from core.rules.checks import RuleDefinitionError
from core.rules.schema import RuleCatalog
from core.service import ConfigValidationService
from mcp_server.server import build_server

LOCAL_ROOT_ENV = "BACKUP_ROOT"
AUDIT_PROFILE_ENV = "AUDIT_PROFILE"


def build_query_service(log: logging.Logger) -> QueryAnalysisService | None:
    """Wire up query analysis, or explain why it is unavailable.

    Optional by design: a deployment with COS access but no cluster
    credentials still serves the config tools rather than refusing to start.
    Missing configuration is logged plainly so the gap is visible in the pod
    log instead of surfacing later as a mysteriously absent tool.
    """
    from adapters.trino_audit import TrinoAuditRepository, TrinoConnectionError

    override = os.environ.get(AUDIT_PROFILE_ENV)
    profile_path = Path(override) if override else default_profile_path()
    try:
        profile = load_profile(profile_path)
    except ProfileError as exc:
        log.warning("Query analysis disabled -- invalid audit profile: %s", exc)
        return None

    try:
        repository = TrinoAuditRepository(profile)
    except TrinoConnectionError as exc:
        log.warning("Query analysis disabled -- %s", exc)
        return None

    log.info(
        "Query analysis enabled against %s (profile %s, %d columns mapped)",
        profile.table.qualified,
        profile.name,
        len(profile.mapped_fields()),
    )
    return QueryAnalysisService(repository)


def build_repository(backend: str, root: str | None) -> ConfigRepository:
    """Construct the configured backup repository."""
    if backend == "local":
        from adapters.local_backup import LocalBackupRepository

        path = root or os.environ.get(LOCAL_ROOT_ENV)
        if not path:
            raise SystemExit(
                f"The local backend needs a directory: pass --root or set "
                f"{LOCAL_ROOT_ENV}."
            )
        return LocalBackupRepository(path)

    from adapters.cos import CosBackupRepository

    return CosBackupRepository()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp_server", description=__doc__)
    parser.add_argument("--backend", choices=("local", "cos"), default="cos")
    parser.add_argument("--root", help="backup directory for the local backend")
    parser.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="stdio"
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--no-query-analysis",
        action="store_true",
        help="serve only the config tools, even if cluster credentials are set",
    )
    args = parser.parse_args(argv)

    # stderr, because stdio transport owns stdout.
    logging.basicConfig(
        level=args.log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("starburst-agent")

    try:
        catalog = RuleCatalog.load()
    except RuleDefinitionError as exc:
        # A malformed catalog must not start: silently evaluating fewer rules
        # is indistinguishable from a healthy fleet.
        log.error("Rule catalog is invalid: %s", exc)
        return 2

    repository = build_repository(args.backend, args.root)
    log.info(
        "Loaded %d rules from %s; serving over %s",
        len(catalog.rules),
        ", ".join(catalog.sources),
        args.transport,
    )

    query_service = None if args.no_query_analysis else build_query_service(log)
    server = build_server(ConfigValidationService(repository, catalog), query_service)
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
