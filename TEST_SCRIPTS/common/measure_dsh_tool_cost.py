"""测量 DSH 会话里**真实请求**的工具定义开销（profile 切换前后的对比依据）。

为什么需要它：`dump_mcp_tools.py` 量的是 MCP 服务器自己吐出的 tools/list，那是"服务端
打算给什么"；本脚本量的是**DSH 真正发出去的那次请求里带了多少工具定义**——两者不同，
中间还隔着客户端。阶段 5 要"出真实对比数据"，依据必须来自真实会话，而不是合成快照。

数据来源：DSH 会话日志 `<DSH_HOME>/sessions/<workspace>/<session>/*.jsonl.zstd` 里的
`request/header` 事件（它记录了该次请求的 config 与完整 tools 数组）。只读，不解压到磁盘。

用法：
    python TEST_SCRIPTS/common/measure_dsh_tool_cost.py                 # 量本工作区全部会话
    python TEST_SCRIPTS/common/measure_dsh_tool_cost.py --limit 3       # 最近 3 个
    python TEST_SCRIPTS/common/measure_dsh_tool_cost.py --out x.json    # 存留痕
    python TEST_SCRIPTS/common/measure_dsh_tool_cost.py --compare a.json b.json

token 估算口径：按 3 字符/token 估（中英混排的经验值）。**这是估算，不是真值**——
真值要看服务端返回的 usage；本脚本的用途是同一口径下的前后对比，不是绝对值。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOME = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")

CHARS_PER_TOKEN = 3   # 估算口径；只用于同口径对比


def _ascii(text) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


def session_files(home: str, workspace: str | None, limit: int) -> list[Path]:
    base = Path(home) / "sessions"
    if workspace:
        dirs = [base / workspace]
    else:
        dirs = [d for d in base.iterdir() if d.is_dir()] if base.is_dir() else []
    files: list[Path] = []
    for d in dirs:
        files.extend(glob.glob(str(d / "*" / "session.v3.jsonl.zstd")))
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return [Path(p) for p in files[:limit]]


def measure(path: Path) -> dict | None:
    try:
        import zstandard
    except ImportError:
        print("zstandard is required: pip install zstandard")
        return None
    d = zstandard.ZstdDecompressor()
    with open(path, "rb") as f:
        raw = d.stream_reader(f).read()
    lines = [l for l in raw.decode("utf-8", "replace").splitlines() if l.strip()]
    recs: list[dict] = []
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") != "request/header":
            continue
        hdr = (o.get("data") or {}).get("header") or {}
        tools = hdr.get("tools") or []
        cfg = hdr.get("config") or {}
        blob = json.dumps(tools, ensure_ascii=False)
        recs.append({
            "session": path.parent.name,
            "provider": cfg.get("provider"),
            "model": cfg.get("model"),
            "tool_count": len(tools),
            "tool_def_chars": len(blob),
            "tool_def_tokens_est": len(blob) // CHARS_PER_TOKEN,
            # 仪器工具单独计数：这正是 profile 切换所改变的那一组
            "instrument_tools": sum(1 for t in tools
                                    if str(t.get("name", "")).startswith("mcp__instrument__")),
            "top_tools": sorted(
                ({"name": t.get("name"),
                  "chars": len(json.dumps(t, ensure_ascii=False))} for t in tools),
                key=lambda x: -x["chars"])[:5],
        })
    if not recs:
        return None
    # ⚠ 取**最新**一条，不是最大的那条：同一会话可能跨 profile 切换（重启后继续同一个会话），
    # 最大值会永远停在切换前那张大表上，拿它做 after 对比会得出"没变化"的错误结论。
    out = dict(recs[-1])
    out["request_count"] = len(recs)
    out["max_tool_def_chars"] = max(r["tool_def_chars"] for r in recs)
    out["max_tool_count"] = max(r["tool_count"] for r in recs)
    out["changed_during_session"] = len({r["tool_count"] for r in recs}) > 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure real tool-definition cost in DSH sessions")
    ap.add_argument("--home", default=DEFAULT_HOME, help="DSH_HOME（默认取环境变量或 ~/.dsh）")
    ap.add_argument("--workspace", default=None, help="只量该工作区目录（默认全部）")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", default=None, help="把结果写成 JSON 留痕")
    ap.add_argument("--compare", nargs=2, default=None, metavar=("OLD", "NEW"),
                    help="对比两份 --out 产物（profile 切换前/后）")
    args = ap.parse_args()

    if args.compare:
        a = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        b = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        ra, rb = a["latest"], b["latest"]
        for tag, r in (("before", ra), ("after ", rb)):
            print(f"{tag}: {r['tool_count']:4d} tools (instrument {r.get('instrument_tools', '?'):>2}) "
                  f"{r['tool_def_chars']:7d} chars ~{r['tool_def_tokens_est']:6d} tok"
                  f"  ({r.get('provider')}/{r.get('model')})")
        if ra["tool_def_chars"]:
            saved = ra["tool_def_tokens_est"] - rb["tool_def_tokens_est"]
            pct = 100.0 * (1 - rb["tool_def_chars"] / ra["tool_def_chars"])
            print(f"saved : ~{saved} tok per request ({pct:.1f}% smaller)")
        if rb.get("changed_during_session"):
            print(f"note  : the 'after' session spans a profile change "
                  f"(tool counts seen: up to {rb.get('max_tool_count')}); "
                  f"using its LATEST request, which is the current state")
        return 0

    files = session_files(args.home, args.workspace, args.limit)
    if not files:
        print(f"no session logs found (home={args.home})")
        return 2

    print(f"{'session':38s} {'tools':>6s} {'instr':>6s} {'chars':>8s} {'~tok':>7s}  model")
    recs = []
    for p in files:
        rec = measure(p)
        if not rec:
            continue
        recs.append(rec)
        flag = "  <- spanned a profile change" if rec.get("changed_during_session") else ""
        print(f"{rec['session'][:38]:38s} {rec['tool_count']:6d} "
              f"{rec.get('instrument_tools', -1):6d} "
              f"{rec['tool_def_chars']:8d} {rec['tool_def_tokens_est']:7d}  "
              f"{_ascii(rec['model'])}{flag}")

    if not recs:
        print("no request/header events in these sessions (likely empty sessions)")
        return 2

    latest = recs[0]   # 列表按 mtime 倒序，第一条即最近使用的会话
    print(f"\nlatest session ({latest['session']}): {latest['tool_count']} tools"
          f" (instrument {latest.get('instrument_tools', '?')}) / "
          f"{latest['tool_def_chars']} chars ~= {latest['tool_def_tokens_est']} tok")
    print(f"  biggest single tools: {_ascii(latest['top_tools'])}")

    if args.out:
        payload = {"measured_at": datetime.now().isoformat(timespec="seconds"),
                   "home": args.home, "workspace": args.workspace,
                   "sessions": recs, "latest": latest}
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"\nartifact: {args.out}")
        print("(measure again with the same --out after switching profile, then --compare)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
