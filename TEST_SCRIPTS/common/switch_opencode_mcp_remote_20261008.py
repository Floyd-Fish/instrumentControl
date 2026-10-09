# -*- coding: utf-8 -*-
"""切换 opencode 全局配置里 `mcp.instruments` 的传输方式：local(stdio) <-> remote(HTTP 单例)。

背景：单例 MCP 服务（`mcp_instruments/run_remote.ps1`）常驻 `127.0.0.1:8765`；
opencode 配 remote 后所有项目共用**一份**进程，取代"每个工作目录各起一个 stdio 子进程"
（本机实测 12 份 × 805 MB ≈ 9.8 GB → 单份 ~67 MB）。

用法：
    python switch_opencode_mcp_remote_20261008.py              # -> remote（默认）
    python switch_opencode_mcp_remote_20261008.py --to local   # -> 回滚 stdio
    python switch_opencode_mcp_remote_20261008.py --dry-run    # 只打印，不写文件

安全：
  * 每次写入前把当前文件备份为 `opencode.json.bak_<ts>`（不覆盖旧备份）；
  * 解析失败就中止，绝不动文件；
  * 写完**回读校验**（不拿"写成功"当结论）；
  * 改完需**重启 opencode** 才生效（重启本身会回收旧的 stdio 子进程）。
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# opencode 全局配置：家目录随机器/用户名变，一律从 Path.home() 派生
CONFIG = str(Path.home() / ".config" / "opencode" / "opencode.json")

REMOTE = {
    "type": "remote",
    "url": "http://127.0.0.1:8765/mcp",
    "oauth": False,          # 本地服务无 OAuth；不开会走授权流程而连不上
    "enabled": True,
}

LOCAL = {
    "command": [
        sys.executable,          # 当前解释器全路径（Windows 下客户端 spawn 需全路径）
        str(ROOT / "mcp_instruments" / "server.py"),
    ],
    "enabled": True,
    "type": "local",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", choices=("remote", "local"), default="remote")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(CONFIG, encoding="utf-8") as f:
        doc = json.load(f)           # 纯 JSON（无注释）；解析失败即中止

    target = REMOTE if args.to == "remote" else LOCAL
    before = doc.get("mcp", {}).get("instruments")
    print("current:", json.dumps(before, ensure_ascii=False))
    print("target :", json.dumps(target, ensure_ascii=False))
    if before == target:
        print("already in target state; nothing to do")
        return 0
    if args.dry_run:
        print("dry-run: no file written")
        return 0

    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = f"{CONFIG}.bak_{ts}"
    shutil.copy2(CONFIG, bak)
    doc.setdefault("mcp", {})["instruments"] = target
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")

    with open(CONFIG, encoding="utf-8") as f:      # 回读校验
        check = json.load(f)["mcp"]["instruments"]
    ok = check == target
    print("backup :", bak)
    print("verify :", "OK" if ok else "MISMATCH!")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
