# starburst-agent

MCP server exposing Starburst/Trino cluster config validation and query
analysis as tools. Consumed by an existing LangGraph parent agent
(migrating to Google ADK at some point). Deployed to OpenShift.

Full design in `docs/SPEC.md`. Read it before starting new work.

## This service contains no LLM

Pure code execution. No LLM calls anywhere in `core/`, `adapters/`, or
`mcp_server/`. Same inputs always produce the same outputs.

The parent agent owns the LLM: it decides which tools to call and writes
the prose. We return structured findings with evidence. If you are about
to add an LLM client to make a judgment call, that judgment belongs in a
rule or a detector instead.

Only permitted model use: embeddings for the optional local doc index,
precomputed at build time and shipped in the image.

## Layering

    core/         pure logic, fully unit-testable, no network at import time
    adapters/     IBM COS client, Trino client, local doc index
    mcp_server/   thin MCP wrapper over core — no business logic here
    tests/

`core/` must not import any agent framework, LLM client, or MCP library.
It is the only part that survives the LangGraph -> ADK migration.

If you find yourself adding an `if framework ==` branch or importing
`langgraph` outside a spike, stop and ask.

## The LLM never evaluates a threshold

Every pass/fail decision is deterministic Python in `core/rules/` and
`core/analysis/detectors/`. Findings must carry evidence (file + line, or
stage + operator id). No finding, no claim.

If no rule fires, the correct output is "no issues found" — never
speculate to fill a report.

## Injected context is non-decisional

Tools accept an optional `context` object (RAG digest from the parent).
It may enrich, annotate, or hint. It must never set a threshold, suppress
a finding, or add one.

Enforce this structurally: compute findings first, then apply enrichment
as a separate pass over the finished list. Context must not be reachable
from evaluation code.

Every rationale carries `rationale_source`: `rule_catalog` |
`injected_context` | `local_index`.

## Tool design

- Scope is an argument, never implicit. No `validate_everything()`.
- Summary before detail: return key properties + anomaly flags first;
  full file contents only when a finding needs them.
- Split anything that loads or mutates a prod cluster into `plan_*` /
  `run_*` so the parent can gate it. Approval logic lives in the parent.
- Tool descriptions are prompts — the parent's model sees only the name,
  description, and schema. Write them for a model, not a human. Say
  explicitly that an empty findings list means no issues.

## OpenShift constraints

- Container runs as a random UID. Never write to `$HOME` or a hardcoded
  path; use `/tmp` or a mounted volume. Set `HOME=/tmp` in the image.
- Base image: `registry.access.redhat.com/ubi9/python-311`.
- Secrets come from mounted files or env vars. Never read a credential
  from a config file in the repo, and never log one.

## Conventions

- Python 3.11, `uv` for deps, `ruff` + `mypy --strict` clean before commit.
- Rules live as YAML in `core/rules/catalog/`, one file per domain, each
  version-gated by SEP version range. Adding a rule must not require
  touching Python.
- Every rule needs a fixture pair in `tests/fixtures/` (one passing,
  one failing config).
- Never commit real config samples. Redact and add to fixtures instead.
