"""审计落盘——通用 SCPI 通道的调用留痕（含被拒绝的记录）。

方案 C 阶段 2：从 `mcp_instruments/server.py` 抽出，使 compact profile / CLI /
代码运行时共用同一个审计出口。行为与原实现一致（同一路径、同一 JSONL 字段、
同一天一文件），仅把仓库根改为可注入（默认按包位置推导，便于离线测试写到临时目录）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

__all__ = ["repo_root", "audit_scpi"]


def repo_root() -> Path:
    """仓库根（`instrument_runtime/` 的上一级）。"""
    return Path(__file__).resolve().parents[1]


def audit_scpi(tool: str, resource: str, cmd: str, *, root: str | Path | None = None,
               **extra) -> str:
    """通用 SCPI 写留痕：TEST_DATA/common/mcp_scpi_audit_YYYYMMDD.jsonl（含拒绝记录）。

    返回写入的文件路径（调用方会把它放进返回体，便于事后定位）。
    """
    base = Path(root) if root is not None else repo_root()
    d = base / "TEST_DATA" / "common"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"mcp_scpi_audit_{datetime.now():%Y%m%d}.jsonl"
    entry = {"ts": datetime.now().isoformat(timespec="seconds"),
             "tool": tool, "resource": resource, "cmd": cmd, **extra}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return str(p)
