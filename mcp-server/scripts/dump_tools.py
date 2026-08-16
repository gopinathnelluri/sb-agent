"""Print the tool manifest exactly as the parent agent's model receives it.

Development aid, not part of the deployed image. Tool descriptions are the
only prompt the parent's model gets for each tool, so being able to read them
as rendered -- with the scope table and shared contract blocks substituted in
-- is the fastest way to catch a description that will mislead it.

    uv run python scripts/dump_tools.py            # descriptions
    uv run python scripts/dump_tools.py --schemas  # descriptions + JSON Schema
"""

from __future__ import annotations

import argparse
import asyncio
import json

from mcp_server.server import build_server
from tests.fakes import FakeConfigService


async def main(show_schemas: bool) -> None:
    server = build_server(FakeConfigService())
    tools = await server.list_tools()
    print(f"{len(tools)} tools\n")
    for tool in tools:
        read_only = tool.annotations and tool.annotations.read_only_hint
        print("=" * 78)
        print(f"{tool.name}{'  [read-only]' if read_only else ''}")
        print("=" * 78)
        print(tool.description or "(no description)")
        if show_schemas:
            print("\n-- input schema --")
            print(json.dumps(tool.input_schema, indent=2))
            print("\n-- output schema --")
            print(json.dumps(tool.output_schema, indent=2))
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schemas", action="store_true", help="include JSON Schema")
    asyncio.run(main(parser.parse_args().schemas))
