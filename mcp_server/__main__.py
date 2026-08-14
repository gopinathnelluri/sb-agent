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

from core.ports import ConfigRepository
from core.rules.checks import RuleDefinitionError
from core.rules.schema import RuleCatalog
from core.service import ConfigValidationService
from mcp_server.server import build_server

LOCAL_ROOT_ENV = "BACKUP_ROOT"


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

    server = build_server(ConfigValidationService(repository, catalog))
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
