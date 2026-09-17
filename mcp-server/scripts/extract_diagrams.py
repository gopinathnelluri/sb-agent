"""Write each mermaid diagram in architecture.md to its own .mmd file.

Standalone files are what a diagram tool, a wiki paste, or a slide wants --
but a second copy of anything drifts, so these are generated rather than
edited. `architecture.md` is the source; run with --check in CI to prove the
two agree.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "architecture.md"
OUT = ROOT / "diagrams"

# Ordered to match the headings in architecture.md. A diagram added there
# without a name here fails loudly rather than landing as `diagram-9.mmd`.
NAMES = (
    "01-components",
    "02-request-flow",
    "03-system-context",
    "04-config-auditor",
    "05-query-analyzer",
    "06-separate-taxonomies",
    "07-deployment",
    "08-layer-imports",
)


def diagrams(text: str) -> list[str]:
    return [
        m.group(1).strip() for m in re.finditer(r"```mermaid\n(.*?)```", text, re.S)
    ]


def main() -> int:
    check = "--check" in sys.argv
    found = diagrams(SOURCE.read_text(encoding="utf-8"))
    if len(found) != len(NAMES):
        print(
            f"architecture.md has {len(found)} diagrams but {len(NAMES)} names "
            f"are configured. Add a name to NAMES in this script.",
            file=sys.stderr,
        )
        return 1

    OUT.mkdir(exist_ok=True)
    stale = []
    for name, body in zip(NAMES, found, strict=True):
        path = OUT / f"{name}.mmd"
        content = body + "\n"
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(path.name)
        else:
            path.write_text(content, encoding="utf-8")

    if check:
        if stale:
            print(
                f"Out of date: {', '.join(stale)}. "
                f"Run: uv run python scripts/extract_diagrams.py",
                file=sys.stderr,
            )
            return 1
        print("diagrams/ is current")
        return 0

    print(f"Wrote {len(NAMES)} diagrams to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
