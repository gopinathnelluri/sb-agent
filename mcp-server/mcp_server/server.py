"""MCP tool surface for use case 1: cluster config validation.

Deliberately thin. Every function here does exactly three things: translate
arguments, call one ``ConfigService`` method, translate errors. No parsing,
no thresholds, no branching on findings. If a change to this file would
alter what counts as a problem, it belongs in ``core/`` instead.

The docstrings are the product. They are the entire prompt the parent's model
sees for each tool -- it gets the name, the description and the schema, and
nothing else. They are written for a model, not for a maintainer.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from inspect import cleandoc
from typing import ParamSpec, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from core.errors import StarburstAgentError
from core.models import (
    ClusterDiff,
    ClusterInfo,
    ConfigDetail,
    ConfigSummary,
    InjectedContext,
    Role,
    RuleRunResult,
)
from core.ports import ConfigService
from core.scopes import SCOPE_GUIDE, Scope

P = ParamSpec("P")
R = TypeVar("R")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)

_EMPTY_FINDINGS_CONTRACT = """
An empty `findings` list means no rule fired: nothing is wrong within the
scopes you asked for. It is a real answer, not a failure, and you should
report it as such. Do not speculate about problems that are not listed.

Check `coverage.complete` before you trust that. If it is false, some rules
did not run -- `coverage.blind_spots` says why in plain language, and you
should pass that caveat on to the user rather than reporting an all clear.
"""


def _translate_errors(fn: Callable[P, R]) -> Callable[P, R]:
    """Turn domain errors into tool errors the caller's model can act on."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except StarburstAgentError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _formatted(fn: Callable[P, R]) -> Callable[P, R]:
    """Substitute the shared documentation blocks into a tool's docstring.

    Applied below ``@server.tool`` so it runs first and the SDK reads the
    finished text. The scope table and the empty-findings contract are
    written once and injected here, so a new scope cannot end up documented
    in one tool and missing from another.
    """
    if fn.__doc__:
        fn.__doc__ = cleandoc(fn.__doc__).format(
            scope_guide=SCOPE_GUIDE,
            empty_contract=_EMPTY_FINDINGS_CONTRACT.strip(),
        )
    return fn


def build_server(service: ConfigService, *, name: str = "starburst-agent") -> MCPServer:
    """Construct the MCP server around a service implementation.

    A factory rather than a module-level singleton so that importing this
    module opens no sockets and reads no credentials -- which is what lets
    the tool layer be unit tested, and keeps import side effects out of a
    container that starts under a random UID.
    """
    server = MCPServer(
        name=name,
        version="0.1.0",
        instructions=(
            "Deterministic Starburst/Trino configuration validation. These "
            "tools read config backups and evaluate a versioned rule catalog. "
            "They contain no model and make no judgement calls: identical "
            "input always yields identical findings, every finding cites a "
            "file and line, and an empty findings list means no issues were "
            "found. Write the prose yourself; these tools supply the facts."
        ),
    )

    @server.tool(annotations=READ_ONLY)
    @_formatted
    @_translate_errors
    def list_clusters() -> list[ClusterInfo]:
        """List the Starburst/Trino clusters that have a config backup available.

        Call this first when the user names a cluster you have not seen, or
        asks which clusters exist. Every other tool takes a `cluster` name
        that must come from this list.

        Each entry reports the detected SEP version and where that detection
        came from (`sep_version_source`). Version matters: the rule catalog is
        version-gated, so a cluster whose version is `unknown` will have rules
        skipped. It also reports `backup_age_days` -- findings describe the
        config as of the backup, not as of now, so mention the age to the user
        when it is more than a few days old.
        """
        return service.list_clusters()

    @server.tool(annotations=READ_ONLY)
    @_formatted
    @_translate_errors
    def get_config_summary(cluster: str, scopes: list[Scope]) -> ConfigSummary:
        """Show the key config properties for one cluster within given scopes.

        Use this to answer "how is this cluster configured" and to ground
        yourself before or after running rules. It returns roughly 20-50
        resolved properties, not whole files, so it is cheap to call.

        `scopes` is required and has no default: decide which domains the
        user's question is about and pass them explicitly. Passing every
        scope to be safe produces a large, unfocused result -- prefer two or
        three. Choose from:

        {scope_guide}

        Each property reports `consistent_across_nodes`. When that is false,
        `differing_nodes` names the nodes that disagree and their values. That
        drift is frequently the actual problem, so surface it rather than
        reporting only the majority value.

        This tool describes configuration. It does not judge it -- to find out
        whether the settings are correct, call `run_rules`.
        """
        return service.get_config_summary(cluster, scopes)

    @server.tool(annotations=READ_ONLY)
    @_formatted
    @_translate_errors
    def run_rules(
        cluster: str,
        scopes: list[Scope],
        context: InjectedContext | None = None,
    ) -> RuleRunResult:
        """Validate a cluster's configuration against the versioned rule catalog.

        This is the main tool for use case 1. Use it whenever the user asks
        whether a cluster is configured correctly, or reports a symptom you
        can map to a config domain. Pick `scopes` from the table below and
        pass them explicitly:

        {scope_guide}

        Every finding carries a `rule_id`, a `severity`, the `actual` and
        `expected` values, a `rationale`, and `evidence` naming the file, node
        and line it came from. Quote the evidence when you explain a finding;
        it is what makes the answer checkable. `findings` is returned already
        sorted, most severe first -- keep that order.

        {empty_contract}

        `context` is optional and non-decisional. If your RAG has relevant
        documentation you may pass it, and it may improve the wording of a
        rationale on a finding that already fired, or add an entry to
        `annotations` about a property no rule covers. It cannot create,
        remove or re-grade a finding: the findings are computed before this
        argument is read. Anything in `annotations` is unvalidated guidance
        and must be presented as such, never as a finding. If supplied
        guidance contradicts the catalog, the disagreement is reported in
        `guidance_conflicts` rather than resolved -- tell the user both
        positions.

        To see the full text of a file behind a finding, call
        `get_config_detail` with the path from that finding's evidence.
        """
        return service.run_rules(cluster, scopes, context)

    @server.tool(annotations=READ_ONLY)
    @_formatted
    @_translate_errors
    def get_config_detail(
        cluster: str, role: Role, file: str, node: str | None = None
    ) -> ConfigDetail:
        """Read the full text of one config file from a cluster backup.

        Use this only when you need context a finding does not already give
        you -- for example to show the surrounding lines of a flagged setting.
        Do not call it to browse: `get_config_summary` is the cheap way to see
        what is configured, and file contents are large.

        Take `role` and `file` from the `evidence` of a finding rather than
        guessing at paths. `node` selects one specific node's copy; omit it
        for the role's representative config. Pass `node` when a finding
        reported node drift and you need to see one outlier's actual file.

        Secrets are verified on the way out. If an unredacted credential is
        detected the call fails rather than returning the file -- that is a
        defect in the backup pipeline. Report it to the user and do not retry.
        """
        return service.get_config_detail(cluster, role, file, node)

    @server.tool(annotations=READ_ONLY)
    @_formatted
    @_translate_errors
    def diff_clusters(
        cluster_a: str, cluster_b: str, scopes: list[Scope]
    ) -> ClusterDiff:
        """Compare two clusters' configuration within the given scopes.

        Use this for "it works on staging but not on production" questions,
        and for verifying that clusters intended to be identical actually are.

        Every difference is classified. `unexpected` means two clusters
        disagree on something that normally matches, and is what you should
        lead with. `expected` covers values that differ by design, such as
        hostnames and node identifiers -- mention them only if asked.
        `unknown` means we have no classification for that property, so use
        judgement and say that you are.

        This reports differences, not correctness. A property can match on
        both clusters and still be wrong on both; run `run_rules` against each
        cluster to find that out.
        """
        return service.diff_clusters(cluster_a, cluster_b, scopes)

    return server
