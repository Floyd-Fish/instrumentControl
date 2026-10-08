"""registry ↔ legacy MCP 工具表 的一致性校验（离线，不碰仪器）。

方案 C 阶段 1 的验收判据（见 docs/gpt_qa/2026-09-23-instrument-gateway-arch.md）：
引入 Operation Registry 必须做到**零行为变更**——68 个工具名、description、
inputSchema 与重构前逐字节一致。本脚本用「重构前固化的 tools 快照」做基准来证明它。

四组断言：
    S1 registry 与 MCP 工具表一一对应（数量、名字集合完全相等）
    S2 每个工具的 description / inputSchema 与基准快照**完全相同**（深比较）
    S3 catalog 覆盖完整：没有未分类的工具，也没有只存在于目录里的残留项
    S4 安全属性自洽：requires_confirm 与函数签名一致；raw_scpi 只在受护栏的原始
       SCPI 通道上；risk 取值合法；canonical id 唯一

用法：
    python TEST_SCRIPTS/common/verify_registry_parity.py [--baseline <path>]
    # 基准默认取 TEST_DATA/common/mcp_tools_baseline_v2dev.json
    # （由 TEST_SCRIPTS/common/dump_mcp_tools.py 在重构前生成）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_instruments"))

# 本脚本比对的是 **legacy 工具面**：S1 直接读 `server.mcp` 的工具表，S2 把 registry 的
# description / inputSchema 与重构前的冻结快照逐字节比对。server.py 的默认档已改为
# compact，不钉住 legacy 的话 `server.mcp` 里只剩 5 个 compact 工具，S1 必然失败。
#
# 这里还承担着**唯一**一道 FastMCP 派生漂移守卫：server.py 只在 legacy 下把
# `Tool.from_function` 的派生结果与真实注册对象比对（compact 下服务实例里没有这些
# 工具，无从比对）。所以本脚本必须显式跑在 legacy 下，并且要跟着 FastMCP 升级一起跑。
os.environ["INSTRUMENT_MCP_PROFILE"] = "legacy"

DEFAULT_BASELINE = ROOT / "TEST_DATA" / "common" / "mcp_tools_baseline_v2dev.json"

fails: list[str] = []


def _ascii(text) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {_ascii(name):54s} {_ascii(detail)[:120]}", flush=True)
    if not ok:
        fails.append(name)


def _canon(obj) -> str:
    """稳定序列化，用于深比较（键序无关）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify registry parity with the legacy tool table")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE),
                    help="frozen tools/list snapshot to compare against")
    args = ap.parse_args()

    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        print(f"baseline snapshot not found: {baseline_path}")
        print("generate it with: python TEST_SCRIPTS/common/dump_mcp_tools.py "
              "--out TEST_DATA/common/mcp_tools_baseline_v2dev.json")
        return 2
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    base_tools = {t["name"]: t for t in baseline["tools"]}

    import inspect

    import server as S  # noqa: E402  (导入即完成全部工具注册与 runtime 登记)

    from instrument_runtime import RISK_LEVELS, get_registry, unclassified

    reg = get_registry()
    live_tools = {t.name: t for t in S.mcp._tool_manager.list_tools()}

    print(f"baseline : {baseline_path.name} ({baseline['tool_count']} tools, "
          f"captured {baseline.get('captured_at')})")
    print(f"registry : {len(reg)} operations | MCP tools: {len(live_tools)}\n")

    # ---------------- S1 一一对应 ----------------
    print("S1 registry <-> MCP tool table is one-to-one")
    check("registry count == MCP tool count", len(reg) == len(live_tools),
          f"registry {len(reg)} / mcp {len(live_tools)}")
    check("registry tool names == MCP tool names",
          set(reg.tool_names()) == set(live_tools),
          f"only-in-registry {sorted(set(reg.tool_names()) - set(live_tools))[:5]} | "
          f"only-in-mcp {sorted(set(live_tools) - set(reg.tool_names()))[:5]}")
    check("registry count == baseline count", len(reg) == baseline["tool_count"],
          f"registry {len(reg)} / baseline {baseline['tool_count']}")
    check("MCP tool names == baseline tool names", set(live_tools) == set(base_tools),
          f"only-in-mcp {sorted(set(live_tools) - set(base_tools))[:5]} | "
          f"only-in-baseline {sorted(set(base_tools) - set(live_tools))[:5]}")

    # ---------------- S2 description / schema 逐字节一致 ----------------
    print("\nS2 description and inputSchema match the baseline exactly")
    desc_bad, schema_bad = [], []
    for name, base in base_tools.items():
        op = reg.get(name)
        if op is None:
            desc_bad.append(name)
            schema_bad.append(name)
            continue
        if op.description != (base.get("description") or ""):
            desc_bad.append(name)
        if _canon(dict(op.schema)) != _canon(base.get("inputSchema") or {}):
            schema_bad.append(name)
    check("every description is byte-identical", not desc_bad,
          f"mismatched {len(desc_bad)}: {desc_bad[:5]}" if desc_bad else f"{len(base_tools)} ok")
    check("every inputSchema is deep-identical", not schema_bad,
          f"mismatched {len(schema_bad)}: {schema_bad[:5]}" if schema_bad else f"{len(base_tools)} ok")

    # ---------------- S3 catalog 覆盖 ----------------
    print("\nS3 catalog coverage is complete (nothing unclassified, nothing stale)")
    stale, unclassified_names = unclassified(reg.tool_names())
    check("no unclassified tools", not unclassified_names, str(unclassified_names[:6]))
    check("no stale catalog entries", not stale, str(stale[:6]))

    # ---------------- S4 安全属性自洽 ----------------
    print("\nS4 safety attributes are self-consistent")
    confirm_bad = []
    for op in reg:
        sig_confirm = "confirm" in inspect.signature(op.fn).parameters
        if sig_confirm != op.safety.requires_confirm:
            confirm_bad.append(op.tool_name)
    check("requires_confirm matches the function signature", not confirm_bad,
          str(confirm_bad[:6]))
    check("declared confirm tools == 10", len([o for o in reg if o.safety.requires_confirm]) == 10,
          f"actual {len([o for o in reg if o.safety.requires_confirm])}")

    bad_risk = [o.tool_name for o in reg if o.safety.risk not in RISK_LEVELS]
    check("every risk level is valid", not bad_risk, str(bad_risk[:6]))

    raw = sorted(o.tool_name for o in reg if o.safety.raw_scpi)
    check("raw_scpi limited to the three guarded SCPI channels",
          raw == ["dg_query", "instr_query", "instr_write"], str(raw))

    ids = [o.id for o in reg]
    check("canonical ids are unique", len(ids) == len(set(ids)),
          f"{len(ids)} ids / {len(set(ids))} unique")

    unknown_device = [o.tool_name for o in reg if not o.device]
    check("every operation has a device family", not unknown_device, str(unknown_device[:6]))

    by_risk: dict[str, int] = {}
    for o in reg:
        by_risk[o.safety.risk] = by_risk.get(o.safety.risk, 0) + 1
    print(f"\n  risk spread : " + ", ".join(f"{k}={by_risk.get(k, 0)}" for k in RISK_LEVELS))
    print(f"  device spread: " + ", ".join(
        f"{k}={len(v)}" for k, v in sorted(reg.by_device().items())))

    print(f"\n== result: {'all PASS' if not fails else f'{len(fails)} FAIL'} ==")
    for f in fails:
        print(f"  - {_ascii(f)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
