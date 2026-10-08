"""Offline meta check for the MCP server: handshake, tool table, descriptions, schemas.

Runs the server as a subprocess and speaks JSON-RPC over stdio. No instrument is
touched (the device tools are never called).

Encoding policy (deliberate, keep it this way):
  * stdout is the protocol stream (UTF-8 JSON-RPC) -> decode as UTF-8.
  * stderr is the server's own startup log; it may be written in the machine's
    legacy console code page. We decode it as UTF-8 with errors="replace" and only
    match **ASCII anchors** ("[instrumentControl]", "description", digits), so this
    check never depends on the local code page.
  * Everything this script prints is ASCII-only, so it runs on any console
    (cp936 / cp437 / utf-8) without reconfiguring streams.

Usage:
    python TEST_SCRIPTS/common/verify_mcp_tools_meta.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "mcp_instruments" / "server.py"
PY_EXE = sys.executable

# 断言对象是 legacy 具名工具面（名字、annotations、description）。server.py 的默认档
# 已改为 compact，不钉住就会对着 5 个 compact 工具做断言。
SERVER_ENV = {**os.environ, "INSTRUMENT_MCP_PROFILE": "legacy"}

fails: list[str] = []


def _ascii(text) -> str:
    """Force ASCII for console safety (non-ASCII becomes '?')."""
    return str(text).encode("ascii", "replace").decode("ascii")


def _is_jsonrpc(line: str) -> bool:
    """True when the stdout line is a well-formed JSON-RPC envelope."""
    try:
        obj = json.loads(line)
    except ValueError:
        return False
    if not isinstance(obj, dict):
        return False
    return obj.get("jsonrpc") == "2.0" and ("result" in obj or "error" in obj or "id" in obj)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {_ascii(name):56s} {_ascii(detail)[:110]}", flush=True)
    if not ok:
        fails.append(name)


def main() -> int:
    # Binary pipes: stdout (UTF-8 JSON-RPC) and stderr (server log, unknown code page)
    # are decoded separately below; one shared `encoding=` cannot serve both.
    proc = subprocess.Popen([PY_EXE, str(SERVER)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=0, cwd=str(ROOT), env=SERVER_ENV)

    def send(obj: dict) -> None:
        proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def read_line(timeout: float = 120.0) -> str:
        box: dict = {}
        th = threading.Thread(
            target=lambda: box.update(l=proc.stdout.readline().decode("utf-8", "replace")),
            daemon=True)
        th.start()
        th.join(timeout)
        return box.get("l", "")

    print("S1 handshake and tool table", flush=True)
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "meta-check", "version": "1"}}})
    line = read_line()
    ok_init = bool(line.strip()) and "result" in (json.loads(line) if line.strip() else {})
    check("initialize returns a result", ok_init, line[:80])
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    raw = read_line()
    tools = json.loads(raw)["result"]["tools"] if raw.strip() else []
    names = [t["name"] for t in tools]
    check("tools/list returns a tool table", len(tools) > 0, f"{len(tools)} tools")
    check("tool names are unique", len(names) == len(set(names)),
          f"{len(names)} names / {len(set(names))} unique")
    check("tool count above floor (>=50)", len(tools) >= 50, f"actual {len(tools)}")

    print("\nS2 description coverage (FastMCP takes them from docstrings)", flush=True)
    no_desc = [t["name"] for t in tools if not (t.get("description") or "").strip()]
    check("every tool has a non-empty description", not no_desc,
          f"missing {len(no_desc)}: {_ascii(no_desc)}" if no_desc else f"{len(tools)}/{len(tools)} ok")

    print("\nS3 inputSchema completeness", flush=True)
    bad_schema = [t["name"] for t in tools
                  if (t.get("inputSchema") or {}).get("type") != "object"]
    check("every tool has an object inputSchema", not bad_schema, str(bad_schema))

    print("\nS4 key tools present (devices + guardrails + fallback)", flush=True)
    expect = ["instr_discover", "instr_query", "instr_write", "usb_reset",
              "sds_status", "sds_auto_scale", "sds_measure", "sdg_set_wave", "sdg_output",
              "dmm_measure", "dho_status", "dho_measure_item",
              "mho_status", "mho_measure_item", "mho_channel", "mho_fit_channel",
              "mho_timebase", "mho_trigger", "psu_status", "psu_output",
              "dg_status", "dg_protect", "dg_output",
              "ks3458a_status", "ks3458a_read", "ks3458a_burst", "ks3458a_reset"]
    missing = [n for n in expect if n not in names]
    check("representative tools present", not missing,
          f"missing {missing}" if missing else f"checked {len(expect)}")

    print("\nS5 startup self-check line (stderr) and stdout purity", flush=True)
    # stdout must carry JSON-RPC only (history: a stray print() into the protocol
    # channel made clients report "Connection closed").
    extra: list[str] = []

    def drain() -> None:
        while True:
            line = proc.stdout.readline()
            if not line:
                return
            extra.append(line.rstrip("\n"))

    th = threading.Thread(target=drain, daemon=True)
    th.start()
    th.join(2.0)
    bad = [l for l in extra if l.strip() and not _is_jsonrpc(l)]
    check("stdout carries JSON-RPC only (no print pollution)", not bad, str(bad[:2]))
    proc.stdin.close()
    try:
        proc.wait(timeout=15)
    except Exception:
        proc.kill()
    # ASCII anchors only: the log text itself may be in a legacy code page.
    err = proc.stderr.read().decode("utf-8", "replace")
    startup = [l for l in err.splitlines() if "[instrumentControl]" in l]
    check("startup self-check line present", bool(startup), startup[0][:100] if startup else "(none)")
    if startup:
        m = re.search(r"description\s+(\d+)", startup[-1])
        check("self-check reports a description gap of 0",
              bool(m) and int(m.group(1)) == 0,
              f"description gap = {m.group(1) if m else 'n/a'}")
    print(f"\n== result: {'all PASS' if not fails else f'{len(fails)} FAIL'} ==")
    for f in fails:
        print(f"  - {_ascii(f)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
