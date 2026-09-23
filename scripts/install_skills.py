#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把仓库 `skills/` 下的 skill 分发到各 AI 工具的 skill 根。

背景：`skills/instrument-mcp/SKILL.md` 是**唯一权威源**。历史上它同时存在于
仓库内（`mcp_instruments/SKILL.md`）与用户级 skill 目录
（`~/.agents/skills/instrument-mcp/`）两处，靠人工同步、容易漂移。本脚本把
"安装"变成一条命令，并默认用**目录链接**安装，从根本上消除漂移。

用法：
    python scripts/install_skills.py                    # 安装（默认 junction/symlink）
    python scripts/install_skills.py --check            # 只报告状态，不动任何文件
    python scripts/install_skills.py --copy             # 复制而不是链接
    python scripts/install_skills.py --uninstall        # 卸载（只动指向本仓的链接）
    python scripts/install_skills.py --root <dir>       # 指定目标根，可重复

默认目标根：`~/.agents/skills`（DSH / Codex / ZCode 共享的通用 skill 根；
AGENTS.md 约定「通用 skill 只放一份」，因此默认不往每个 harness 各装一份）。

安全约定（与本仓 AGENTS.md 一致）：
  * 目标已存在且**内容与源不一致**时**拒绝**安装（保护本地修改），除非显式 --force，
    且 --force 会先把原目录完整备份到 `TEST_DATA/common/`；
  * `--check` 不改动任何文件；
  * `--uninstall` 只删除「指向本仓的链接」或「与源逐字节一致的副本」，不碰有改动的目录。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "skills"
DEFAULT_ROOTS = [Path.home() / ".agents" / "skills"]
BACKUP_ROOT = ROOT / "TEST_DATA" / "common"

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def log(msg: str = "") -> None:
    print(msg, flush=True)


def skill_sources() -> list[tuple[str, Path]]:
    """仓库内可分发的 skill：`skills/<name>/SKILL.md` 存在。"""
    if not SRC_DIR.is_dir():
        return []
    out = []
    for d in sorted(SRC_DIR.iterdir()):
        if d.is_dir() and (d / "SKILL.md").is_file():
            out.append((d.name, d))
    return out


def tree_hash(d: Path) -> str:
    """目录内容哈希（按相对路径排序，含路径名与字节内容）。"""
    h = hashlib.sha256()
    for f in sorted(d.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(d)).replace("\\", "/").encode("utf-8"))
            h.update(b"\0")
            h.update(f.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def is_reparse_point(p: Path) -> bool:
    """目录链接（Windows junction / symlink，或 POSIX symlink）。"""
    try:
        if p.is_symlink():
            return True
        attrs = getattr(os.lstat(p), "st_file_attributes", 0)
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def points_to(p: Path, target: Path) -> bool:
    """p 是否是指向 target 的链接（realpath 比较，兼容 junction 与 symlink）。"""
    if not is_reparse_point(p):
        return False
    try:
        return os.path.realpath(p) == os.path.realpath(target)
    except OSError:
        return False


def make_link(link: Path, target: Path) -> None:
    """建目录链接：Windows 用 junction（mklink /J，不需要管理员权限），其余用 symlink。"""
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise OSError(f"mklink /J 失败: {r.stdout.strip()} {r.stderr.strip()}")
    else:
        os.symlink(target, link, target_is_directory=True)


def remove_link(p: Path) -> None:
    """删除一个**目录链接**（junction 或 symlink），绝不触碰它指向的目标。

    刻意*不*用 `Path.is_symlink()` 判断：2026-09-23 实测，Windows junction 在
    CPython 上 `Path.is_symlink()` 与 `os.path.islink()` **都返回 False**，只有
    `st_file_attributes` 带 reparse 位才认得出来（见
    `TEST_SCRIPTS/common/verify_install_skills_uninstall.py` §6 的 [INFO] 行）。
    这里先用 `os.rmdir`（对目录链接只摘链接本身、不递归），失败再退回 `unlink`
    （真 symlink）—— 这条路径不依赖"哪个 API 认 junction"的版本假设，判错一次
    的代价是递归删掉源目录，所以宁可写得笨。
    """
    try:
        os.rmdir(p)
    except (NotADirectoryError, FileNotFoundError):
        p.unlink()


def backup(dst: Path, stamp: str) -> Path:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    out = BACKUP_ROOT / f"skill_backup_{dst.name}_{stamp}"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(dst, out)
    return out


def status_of(dst: Path, src: Path) -> tuple[str, str]:
    """返回 (状态码, 说明)。状态码 ∈ missing/linked/foreign/changed/same/blocked。"""
    if not dst.exists() and not is_reparse_point(dst):
        return "missing", "未安装"
    if is_reparse_point(dst):
        if points_to(dst, src):
            return "linked", f"链接 -> {src}"
        return "foreign", f"链接指向别处 -> {os.path.realpath(dst)}"
    if not dst.is_dir():
        return "blocked", "目标不是目录"
    if tree_hash(dst) == tree_hash(src):
        return "same", "副本内容与源一致"
    return "changed", "副本内容与源不一致（本地有改动？）"


def install_one(name: str, src: Path, root: Path, mode: str, force: bool,
                do_uninstall: bool, stamp: str) -> str:
    dst = root / name
    st, desc = status_of(dst, src)

    if do_uninstall:
        if st == "linked":
            remove_link(dst)
            return "已卸载（链接移除）"
        if st == "same":
            shutil.rmtree(dst)
            return "已卸载（副本删除）"
        if st == "missing":
            return "跳过（未安装）"
        return f"拒绝卸载：{desc}"

    if st == "linked":
        return "已安装（链接有效，跳过）"
    if st == "same" and mode == "link":
        # 内容一致：安全升级为链接
        shutil.rmtree(dst)
        make_link(dst, src)
        return "已改为链接（原副本内容一致）"
    if st == "same":
        return "已安装（副本一致，跳过）"
    if st == "foreign":
        if not force:
            return f"拒绝：{desc}（--force 可覆盖，会先备份）"
        bak = backup(dst, stamp)
        if is_reparse_point(dst):
            remove_link(dst)
        else:
            shutil.rmtree(dst)
        _place(dst, src, mode)
        return f"已覆盖（原目标备份到 {bak.relative_to(ROOT)}）"
    if st == "changed":
        if not force:
            return f"拒绝：{desc}（--force 可覆盖，会先备份）"
        bak = backup(dst, stamp)
        shutil.rmtree(dst)
        _place(dst, src, mode)
        return f"已覆盖（原目录备份到 {bak.relative_to(ROOT)}）"
    if st == "blocked":
        return f"拒绝：{desc}"
    # missing
    _place(dst, src, mode)
    return "已安装（" + ("链接" if mode == "link" else "副本") + "）"


def _place(dst: Path, src: Path, mode: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if mode == "link":
        make_link(dst, src)
    else:
        shutil.copytree(src, dst)


def main() -> int:
    ap = argparse.ArgumentParser(description="把仓库 skills/ 分发到 AI 工具的 skill 根")
    ap.add_argument("--root", action="append", default=None,
                    help="目标 skill 根，可重复；默认 ~/.agents/skills")
    ap.add_argument("--copy", action="store_true", help="复制而不是建链接")
    ap.add_argument("--link", action="store_true", help="建目录链接（默认）")
    ap.add_argument("--check", action="store_true", help="只报告状态，不改动任何文件")
    ap.add_argument("--uninstall", action="store_true", help="卸载本仓分发的 skill")
    ap.add_argument("--force", action="store_true",
                    help="目标有本地改动时仍覆盖（会先完整备份到 TEST_DATA/common/）")
    args = ap.parse_args()

    mode = "copy" if args.copy else "link"
    roots = [Path(r).expanduser().resolve() for r in (args.root or DEFAULT_ROOTS)]
    srcs = skill_sources()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    log(f"仓库根   : {ROOT}")
    log(f"skill 源 : {SRC_DIR}")
    log(f"目标根   : {', '.join(str(r) for r in roots)}")
    log(f"模式     : {'只报告 --check' if args.check else ('卸载' if args.uninstall else mode)}")
    log("")
    if not srcs:
        log(f"!! {SRC_DIR} 下没有找到 <name>/SKILL.md")
        return 2

    rc = 0
    for root in roots:
        log(f"[{root}]")
        for name, src in srcs:
            dst = root / name
            st, desc = status_of(dst, src)
            if args.check:
                mark = {"linked": "OK ", "same": "OK ", "missing": "-- ",
                        "foreign": "!! ", "changed": "!! ", "blocked": "!! "}[st]
                if st in ("foreign", "changed", "blocked"):
                    rc = 1
                log(f"  {mark}{name:<20} {desc}")
                continue
            result = install_one(name, src, root, mode, args.force, args.uninstall, stamp)
            if result.startswith("拒绝"):
                rc = 1
            log(f"  {name:<20} {result}")
        log("")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
