# Diagrams

Mermaid source, one file per diagram. **Generated — do not edit.**
`architecture.md` in the repo root is the source, and it carries the prose
that explains each one.

To regenerate after changing `architecture.md`:

    cd mcp-server && uv run python scripts/extract_diagrams.py

To verify the two agree (CI):

    cd mcp-server && uv run python scripts/extract_diagrams.py --check

| File | What it shows |
|---|---|
| `01-components.mmd` | The pieces and what talks to what |
| `02-request-flow.mmd` | One request, end to end |
| `03-system-context.mmd` | Which side of the boundary owns the model |
| `04-config-auditor.mmd` | Use case 1, backup to finding |
| `05-query-analyzer.mmd` | Use case 2, audit row to root cause |
| `06-separate-taxonomies.mmd` | Why a query finding cannot carry a config scope |
| `07-deployment.mmd` | OpenShift, secrets, read-only surface |
| `08-layer-imports.mmd` | What each layer may import |

The first two are the ones to open first; the rest are detail.
