"""Dump the instrument MCP server's full tools/list table to a JSON snapshot.

Purpose: freeze a byte-exact baseline of the 57 tool definitions (name,
description, inputSchema) so a later refactor can prove parity. No instrument is
touched -- the device tools are never called; only the MCP handshake and
tools/list are exercised.

Encoding policy (same as verify_mcp_tools_meta.py, deliberate):
  * stdout is the protocol stream (UTF-8 JSON-RPC) -> decode as UTF-8.
  * stderr is the server's own startup log and may use the legacy console code
    page -> decoded separately, only ASCII anchors are ever matched.
  * What this script prints is ASCII-only.

Usage:
    python TEST_SCRIPTS/common/dump_mcp_tools.py [--out <path>]
    # default: TEST_DATA/common/mcp_tools_snapshot_<YYYYmmdd_HHMMSS>.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "mcp_instruments" / "server.py"
PY_EXE = sys.executable

# 快照的对象是 **legacy 具名工具面**（68 个工具）。server.py 的默认档已改为 compact，
# 不在这里钉住就会只抓到 5 个 compact 工具，基准快照失真而校验脚本无从察觉。
SERVER_ENV = {**os.environ, "INSTRUMENT_MCP_PROFILE": "legacy"}


def _ascii(text) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


class ServerProbe:
    """Minimal stdio JSON-RPC client for the MCP server."""

    def __init__(self, timeout: float = 180.0) -> None:
        self.timeout = timeout
        self.proc = subprocess.Popen(
            [PY_EXE, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=str(ROOT),
            env=SERVER_ENV,
        )

    def send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def read_line(self, timeout: float | None = None) -> str:
        assert self.proc.stdout is not None
        box: dict = {}

        def _read() -> None:
            box["l"] = self.proc.stdout.readline().decode("utf-8", "replace")

        th = threading.Thread(target=_read, daemon=True)
        th.start()
        th.join(timeout if timeout is not None else self.timeout)
        return box.get("l", "")

    def handshake(self) -> dict:
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                              "clientInfo": {"name": "tools-dump", "version": "1"}}})
        line = self.read_line()
        if not line.strip():
            raise RuntimeError("initialize timed out (no response on stdout)")
        init = json.loads(line)
        if "result" not in init:
            raise RuntimeError(f"initialize failed: {line[:200]}")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return init["result"]

    def tools(self) -> list[dict]:
        self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        line = self.read_line()
        if not line.strip():
            raise RuntimeError("tools/list timed out (no response on stdout)")
        obj = json.loads(line)
        if "result" not in obj:
            raise RuntimeError(f"tools/list failed: {line[:200]}")
        return obj["result"]["tools"]

    def close(self) -> str:
        """Close stdin and collect the stderr startup log (may be non-UTF-8)."""
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except Exception:
            self.proc.kill()
        assert self.proc.stderr is not None
        return self.proc.stderr.read().decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser(description="Dump MCP tools/list to a JSON snapshot")
    ap.add_argument("--out", default=None, help="output path (default: timestamped under TEST_DATA/common)")
    args = ap.parse_args()

    if args.out:
        out = Path(args.out)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = ROOT / "TEST_DATA" / "common" / f"mcp_tools_snapshot_{ts}.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    probe = ServerProbe()
    try:
        info = probe.handshake()
        tools = probe.tools()
    finally:
        log = probe.close()

    # Sort by name so the snapshot is diffable regardless of registration order.
    tools_sorted = sorted(tools, key=lambda t: t["name"])
    payload = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "server": str(SERVER.relative_to(ROOT)),
        "server_info": info.get("serverInfo", {}),
        "protocol_version": info.get("protocolVersion"),
        "tool_count": len(tools_sorted),
        "tools": tools_sorted,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"server      : {_ascii(info.get('serverInfo', {}).get('name'))} "
          f"{_ascii(info.get('serverInfo', {}).get('version'))}")
    print(f"tools       : {len(tools_sorted)}")
    print(f"snapshot    : {_ascii(out)}")
    print(f"size        : {out.stat().st_size} bytes")
    startup = [l for l in log.splitlines() if "[instrumentControl]" in l]
    for line in startup:
        print(f"startup     : {_ascii(line.strip())[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
