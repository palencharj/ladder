"""CI smoke test: does the MCP server actually speak the protocol?

Spawns the server as a subprocess and drives a real handshake over stdio, the
same way Claude Code would. Catches the class of breakage unit tests miss --
a stray print to stdout, a malformed schema, a tool that fails to register.

Exits non-zero with a readable reason on failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp" / "ladder_mcp.py"

EXPECTED_TOOLS = {
    "ladder_health", "ladder_tiers", "ladder_run",
    "ladder_swarm", "ladder_review", "ladder_status", "ladder_stats", "ladder_report", "ladder_models",
}

REQUESTS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "ci", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "ladder_tiers", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "ladder_stats", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "no_such_tool", "arguments": {}}},
]


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    payload = "".join(json.dumps(r) + "\n" for r in REQUESTS)
    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input=payload, capture_output=True, text=True, timeout=120, cwd=ROOT,
    )
    if proc.returncode != 0:
        fail(f"server exited {proc.returncode}\nstderr:\n{proc.stderr[:2000]}")

    responses = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            fail(f"non-JSON on stdout (stdout is the protocol channel): {line[:200]}")
        if "id" in msg:
            responses[msg["id"]] = msg

    # initialize
    init = responses.get(1) or fail("no response to initialize")
    result = init.get("result") or fail(f"initialize errored: {init}")
    for key in ("protocolVersion", "capabilities", "serverInfo"):
        if key not in result:
            fail(f"initialize result missing {key}")

    # tools/list
    listed = responses.get(2) or fail("no response to tools/list")
    tools = listed.get("result", {}).get("tools")
    if not tools:
        fail(f"tools/list returned nothing: {listed}")
    names = {t["name"] for t in tools}
    if missing := EXPECTED_TOOLS - names:
        fail(f"tools missing from registry: {sorted(missing)}")
    for tool in tools:
        for key in ("name", "description", "inputSchema"):
            if key not in tool:
                fail(f"tool {tool.get('name', '?')} missing {key}")
        if tool["inputSchema"].get("type") != "object":
            fail(f"tool {tool['name']} inputSchema must be type=object")

    # tool calls return content
    for rid, label in ((3, "ladder_tiers"), (4, "ladder_stats")):
        resp = responses.get(rid) or fail(f"no response for {label}")
        content = resp.get("result", {}).get("content")
        if not content or not content[0].get("text"):
            fail(f"{label} returned no text content: {resp}")

    # unknown tool must be reported, not crash the server
    unknown = responses.get(5) or fail("no response for unknown tool")
    if "error" not in unknown and not unknown.get("result", {}).get("isError"):
        fail(f"unknown tool should surface an error, got: {unknown}")

    print(f"MCP handshake OK: {len(names)} tools registered ({', '.join(sorted(names))})")


if __name__ == "__main__":
    main()
