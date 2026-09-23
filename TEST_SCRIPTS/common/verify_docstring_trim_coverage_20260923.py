# -*- coding: utf-8 -*-
"""精简后的信息守恒核对（配合 trim_mcp_tool_docstrings_20260923.py 使用）。

用途：证明"description 精简"只是把内容挪进 skill，而非删除。
做法：对本次改动的每个工具，取「旧 description − 新 description」的差集，
      提取其中的**可机械识别信息原子**（文档路径、绝对路径、SCPI 命令/标识符、
      数值与单位、手册页码），逐个检查是否仍出现在

          ① 新 description，或
          ② skill instrument-mcp 的 SKILL.md

      任何一处。未被覆盖的原子逐条打出来，供人工判断。

局限（如实说明）：本脚本只覆盖**非中文**信息原子。中文语义（如"为什么必须是 IFC"、
"静默忽略"等表述）无法机械比对，由改动时逐条人工对照 SKILL.md 完成；本脚本是
补充证据，不是全部证据。

用法：
    python TEST_SCRIPTS/common/verify_docstring_trim_coverage_20260923.py \
        --old <旧 description 文本> [--new-desc <新 description 文本>]
    默认从 git HEAD 取旧 server.py、从工作区取新 server.py 与 SKILL.md。
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "mcp_instruments" / "server.py"
SKILL = Path.home() / ".agents" / "skills" / "instrument-mcp" / "SKILL.md"

# 可机械识别的信息原子
TOKEN_RE = re.compile(
    r"""
      docs/[A-Za-z0-9_\-./]+                      # 仓库内文档路径
    | [A-Za-z]:[\\/][^\s，。）)、]+                # 绝对路径
    | [A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z0-9_]+)+\??  # 带冒号的命令/标识（SYST:ERR?）
    | \b[A-Za-z]{2,}\d+[A-Za-z]*\b                 # 型号类（SDS824X、MHO984D、3458A）
    | \d+(?:\.\d+)?(?:E[+-]?\d+)?(?:%|s|ms|V|Hz|GSa/s|bit|字节|项|轮)?   # 数值+单位
    """,
    re.X,
)

# 这些是编辑过程中有意替换/删除的通用词，不算信息原子
STOPWORDS = {
    "instr", "resource", "tool", "tools", "skill", "MCP", "ok", "true", "false",
    "None", "json", "csv", "CSV", "PNG", "mcp", "and", "the", "of",
}


def docstrings(src: str) -> dict[str, str]:
    tree = ast.parse(src)
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.decorator_list:
            continue
        names = []
        for d in node.decorator_list:
            if isinstance(d, ast.Name):
                names.append(d.id)
            elif isinstance(d, ast.Attribute):
                names.append(d.attr)
            elif isinstance(d, ast.Call):
                f = d.func
                names.append(f.id if isinstance(f, ast.Name) else getattr(f, "attr", "?"))
        if not any(n in ("device_tool", "tool") for n in names):
            continue
        b0 = node.body[0]
        if (isinstance(b0, ast.Expr) and isinstance(b0.value, ast.Constant)
                and isinstance(b0.value.value, str)):
            out[node.name] = b0.value.value
    return out


def atoms(text: str) -> set[str]:
    return {t for t in TOKEN_RE.findall(text) if t not in STOPWORDS and len(t) > 1}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-rev", default="HEAD", help="取旧 server.py 的 git 版本（默认 HEAD）")
    args = ap.parse_args()

    old_src = subprocess.run(
        ["git", "show", f"{args.old_rev}:mcp_instruments/server.py"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout
    old_docs = docstrings(old_src)
    new_docs = docstrings(SERVER.read_text(encoding="utf-8"))
    skill = SKILL.read_text(encoding="utf-8")

    changed = [n for n in new_docs if n in old_docs and old_docs[n] != new_docs[n]]
    print(f"改动工具 {len(changed)} 个；SKILL.md = {SKILL}")
    print(f"旧 server.py 取自 git {args.old_rev}\n")

    total_atoms = 0
    total_missing = 0
    report = []
    for name in sorted(changed):
        removed = atoms(old_docs[name]) - atoms(new_docs[name])
        where = new_docs[name] + "\n" + skill
        missing = sorted(t for t in removed if t not in where)
        total_atoms += len(removed)
        total_missing += len(missing)
        report.append((name, len(removed), missing))

    print(f"{'tool':<26}{'被移出原子':>10}{'未覆盖':>8}")
    for name, n, missing in report:
        flag = "" if not missing else "  <<<"
        print(f"{name:<26}{n:>10}{len(missing):>8}{flag}")
    print(f"\n合计：移出信息原子 {total_atoms} 个，未在 新description/SKILL.md 中命中 {total_missing} 个")

    if total_missing:
        print("\n=== 未覆盖明细（需人工确认是否属有意删除）===")
        for name, n, missing in report:
            if missing:
                print(f"\n[{name}]")
                for t in missing:
                    print(f"   - {t}")
        return 1
    print("\n[PASS] 所有可机械识别的信息原子均已在新 description 或 SKILL.md 中保留。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
