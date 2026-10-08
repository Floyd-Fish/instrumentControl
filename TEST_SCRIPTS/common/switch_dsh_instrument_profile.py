"""切换 DSH 的 instrument MCP profile（compact / legacy），带备份、校验与回滚。

为什么需要它：切 profile 要改 `~/.dsh/profiles/web/cordis.patch.yml`，而那属于**外部
目录**——按仓库规则，AI 不应擅自改动；由使用者显式执行才是正确路径。既然要人来执行，
就把风险压到最低：本脚本负责备份、精确定位、YAML 校验与幂等，避免手改 YAML 出错
（那文件注释很密，且缩进是语义的一部分）。

做法是**逐行文本编辑**，不做 YAML 往返：往返会抹掉全部注释，而这个文件的价值有一半在注释里。

用法：
    # 先看会改什么（不写盘）
    python TEST_SCRIPTS/common/switch_dsh_instrument_profile.py --profile compact --dry-run
    # 真正写入（自动备份）
    python TEST_SCRIPTS/common/switch_dsh_instrument_profile.py --profile compact --apply
    # 回滚
    python TEST_SCRIPTS/common/switch_dsh_instrument_profile.py --profile legacy --apply

改完**必须重启 DSH** 才生效：MCP 子进程的工具表不会热重载。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".dsh" / "profiles" / "web" / "cordis.patch.yml"
ENTRY_ID = "mcp-instrument"
KEY = "INSTRUMENT_MCP_PROFILE"
PROFILES = ("compact", "legacy")


def _ascii(text) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


def _yaml_ok(text: str) -> tuple[bool, str]:
    """校验 YAML 可解析。DSH 的 `!!js` 是自定义标签，这里注册一个宽容构造器。"""
    try:
        import yaml
    except ImportError:
        return True, "pyyaml not installed; skipped"
    class L(yaml.SafeLoader):
        pass
    L.add_constructor("tag:yaml.org,2002:js", lambda l, n: l.construct_scalar(n))
    try:
        yaml.load(text, Loader=L)
        return True, "ok"
    except Exception as e:                                       # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _find_entry(lines: list[str]) -> tuple[int, int]:
    """返回 mcp-instrument 条目的 (起, 止) 行号区间（0-based，止为开区间）。

    条目边界 = 下一条同缩进的 `- id:`，或文件结束。
    """
    start = None
    for i, ln in enumerate(lines):
        if re.match(rf"^\s*-\s*id:\s*{re.escape(ENTRY_ID)}\s*$", ln):
            start = i
            break
    if start is None:
        raise SystemExit(f"REFUSED: entry '- id: {ENTRY_ID}' not found in config")
    indent = len(lines[start]) - len(lines[start].lstrip())
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if not ln.strip():
            continue
        cur = len(ln) - len(ln.lstrip())
        if cur == indent and re.match(r"^\s*-\s*id:", ln):
            return start, j
    return start, len(lines)


def _env_block(lines: list[str], start: int, end: int) -> tuple[int, int, str]:
    """在条目区间内找 env: 及其子键范围；返回 (env 行号, 子键结束行号, 子键缩进)。"""
    env_i = None
    for i in range(start, end):
        if re.match(r"^\s*env:\s*$", lines[i]):
            env_i = i
            break
    if env_i is None:
        raise SystemExit(f"REFUSED: entry '{ENTRY_ID}' has no 'env:' block to extend")
    env_indent = len(lines[env_i]) - len(lines[env_i].lstrip())
    k = env_i + 1
    child_indent = None
    while k < end:
        ln = lines[k]
        if not ln.strip():
            k += 1
            continue
        cur = len(ln) - len(ln.lstrip())
        if cur <= env_indent:
            break
        if child_indent is None:
            child_indent = cur
        k += 1
    return env_i, k, " " * (child_indent if child_indent is not None else env_indent + 2)


def rewrite(text: str, profile: str) -> tuple[str, str]:
    """返回 (新文本, 动作说明)。幂等：已经是目标状态时原样返回。"""
    lines = text.splitlines(keepends=True)
    start, end = _find_entry(lines)
    env_i, env_end, indent = _env_block(lines, start, end)

    existing = [i for i in range(env_i + 1, env_end)
                if re.match(rf"^\s*{KEY}\s*:", lines[i])]

    if profile == "legacy":
        if not existing:
            return text, "already legacy (no %s line); nothing to do" % KEY
        for i in reversed(existing):
            del lines[i]
        return "".join(lines), f"removed {KEY} line -> legacy"

    # compact
    newline = f"{indent}{KEY}: compact\n"
    if existing:
        i = existing[0]
        if re.match(rf"^\s*{KEY}\s*:\s*compact\s*$", lines[i]):
            return text, "already compact; nothing to do"
        lines[i] = newline
        for j in reversed(existing[1:]):
            del lines[j]
        return "".join(lines), f"updated {KEY} -> compact"
    # 插到 env 块**最后一个非空子键之后**（不能落在块尾空行之后：本文件靠注释与版式
    # 传达结构，插到空行之后会让人误以为这个键属于下一个条目）。
    insert_at = env_end
    while insert_at - 1 > env_i and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, newline)
    return "".join(lines), f"inserted {KEY}: compact under env:"


def main() -> int:
    ap = argparse.ArgumentParser(description="Switch DSH instrument MCP profile")
    ap.add_argument("--profile", required=True, choices=PROFILES)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只预览）")
    ap.add_argument("--dry-run", action="store_true", help="只预览（默认行为，显式写出来更清楚）")
    args = ap.parse_args()

    cfg = Path(args.config)
    if not cfg.exists():
        print(f"REFUSED: config not found: {_ascii(cfg)}")
        return 2
    text = cfg.read_text(encoding="utf-8")

    new_text, action = rewrite(text, args.profile)
    print(f"config : {_ascii(cfg)}")
    print(f"action : {_ascii(action)}")

    if new_text == text:
        print("no change needed.")
        return 0

    ok, why = _yaml_ok(new_text)
    if not ok:
        print(f"REFUSED: rewritten YAML does not parse ({_ascii(why)}); file left untouched")
        return 3
    print(f"yaml   : {_ascii(why)}")

    # 差异预览
    old_lines, new_lines = text.splitlines(), new_text.splitlines()
    import difflib
    diff = list(difflib.unified_diff(old_lines, new_lines, "before", "after", lineterm="", n=1))
    for d in diff[:20]:
        print("   " + _ascii(d))

    if not args.apply:
        print("\n(dry run; re-run with --apply to write)")
        return 0

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = cfg.with_name(cfg.name + f".before-instrument-profile-{ts}")
    shutil.copy2(cfg, backup)
    cfg.write_text(new_text, encoding="utf-8", newline="")
    print(f"\nwritten; backup: {_ascii(backup)}")
    print("NEXT: restart DSH -- the MCP tool table does NOT hot-reload.")
    if args.profile == "compact":
        print("      after restart, verify with:")
        print("        python TEST_SCRIPTS/common/measure_dsh_tool_cost.py "
              "--workspace=--D-ChatWorkspace-- --out after.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
