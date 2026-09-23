# -*- coding: utf-8 -*-
"""`scripts/install_skills.py` 的安装/卸载安全性回归（纯离线、不碰真实 skill 根）。

为什么单独有这个套件：分发默认用**目录链接**（Windows junction）安装，而
"删除一个链接"和"删除链接背后的目录"在 Windows 上边界很细 —— 判错一次就会
递归删掉源目录，代价远高于 skill 漂移。本套件把这条路径钉死在断言上。

判据（每一条都是"删错了就会 FAIL"的硬断言）：
  §1 链接安装后：目标被识别为链接、指向源、源完好
  §2 `--uninstall` 之后：**源目录逐字节不变**、目标链接消失、源文件仍可读
  §3 复制模式安装 + 卸载：只删副本、源完好
  §4 目标是被本地改过的副本时：**拒绝卸载**（保护本地修改），且源与目标都还在
  §5 目标是指向别处的陌生链接时：**拒绝卸载**，链接与源都完好
  §6 `is_reparse_point` / `points_to` 对 junction 的判定与实际一致

用法：python TEST_SCRIPTS/common/verify_install_skills_uninstall.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import install_skills as ins  # noqa: E402

fails: list[str] = []
passed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"  [PASS] {name:62s} {detail[:60]}", flush=True)
    else:
        fails.append(name)
        print(f"  [FAIL] {name:62s} {detail[:60]}", flush=True)


def tree_hash(p: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(p)).encode("utf-8"))
            h.update(f.read_bytes())
    return h.hexdigest()


def fresh_root(tmp: Path, tag: str) -> Path:
    r = tmp / f"root-{tag}"
    r.mkdir(parents=True, exist_ok=True)
    return r


def main() -> int:
    src_name = "instrument-mcp"
    src = ROOT / "skills" / src_name
    if not (src / "SKILL.md").is_file():
        print(f"!! 找不到源 skill: {src}")
        return 2
    src_hash_before = tree_hash(src)
    src_mtime_before = (src / "SKILL.md").stat().st_mtime_ns

    tmp = Path(tempfile.mkdtemp(prefix="skill-install-verify-"))
    print(f"临时工作区: {tmp}")
    print(f"源 skill   : {src}\n")

    # ── §1 链接安装 ────────────────────────────────────────────────────────
    print("§1 链接安装")
    root = fresh_root(tmp, "link")
    root.mkdir(parents=True, exist_ok=True)
    msg = ins.install_one(src_name, src, root, "link", False, False, "verify")
    dst = root / src_name
    check("安装返回成功信息", "已安装" in msg, msg)
    check("目标被识别为链接", ins.is_reparse_point(dst), f"is_reparse_point={ins.is_reparse_point(dst)}")
    check("目标指向源", ins.points_to(dst, src), f"realpath={os.path.realpath(dst)}")
    check("status_of 判为 linked", ins.status_of(dst, src)[0] == "linked", ins.status_of(dst, src)[1])
    check("通过链接能读到 SKILL.md", (dst / "SKILL.md").is_file())

    # ── §2 卸载链接（本套件的核心断言）────────────────────────────────────
    print("\n§2 卸载链接 —— 源必须毫发无损")
    msg = ins.install_one(src_name, src, root, "link", False, True, "verify")
    check("卸载返回成功信息", "已卸载" in msg, msg)
    check("源目录仍存在", src.is_dir())
    check("源 SKILL.md 仍可读", (src / "SKILL.md").is_file())
    check("源内容逐字节未变", tree_hash(src) == src_hash_before,
          f"{tree_hash(src)[:12]} vs {src_hash_before[:12]}")
    check("源 SKILL.md mtime 未变", (src / "SKILL.md").stat().st_mtime_ns == src_mtime_before)
    check("目标链接已消失", not dst.exists() and not ins.is_reparse_point(dst))

    # ── §3 复制模式安装 + 卸载 ─────────────────────────────────────────────
    print("\n§3 复制模式安装 + 卸载")
    root = fresh_root(tmp, "copy")
    root.mkdir(parents=True, exist_ok=True)
    msg = ins.install_one(src_name, src, root, "copy", False, False, "verify")
    dst = root / src_name
    check("安装返回成功信息", "已安装" in msg, msg)
    check("目标不是链接（真副本）", not ins.is_reparse_point(dst))
    check("副本内容与源一致", tree_hash(dst) == src_hash_before)
    msg = ins.install_one(src_name, src, root, "copy", False, True, "verify")
    check("卸载返回成功信息", "已卸载" in msg, msg)
    check("源目录仍存在且未变", src.is_dir() and tree_hash(src) == src_hash_before)
    check("副本已删除", not dst.exists())

    # ── §4 本地改过的副本 → 拒绝卸载 ───────────────────────────────────────
    print("\n§4 本地改过的副本 —— 必须拒绝卸载")
    root = fresh_root(tmp, "modified")
    root.mkdir(parents=True, exist_ok=True)
    ins.install_one(src_name, src, root, "copy", False, False, "verify")
    dst = root / src_name
    with open(dst / "SKILL.md", "a", encoding="utf-8") as f:
        f.write("\n<!-- local edit -->\n")
    msg = ins.install_one(src_name, src, root, "copy", False, True, "verify")
    check("拒绝卸载", msg.startswith("拒绝"), msg)
    check("被改过的副本仍在", dst.is_dir())
    check("源仍完好", tree_hash(src) == src_hash_before)

    # ── §5 陌生链接 → 拒绝卸载 ─────────────────────────────────────────────
    print("\n§5 指向别处的陌生链接 —— 必须拒绝卸载")
    other = tmp / "other-skill"
    other.mkdir(parents=True, exist_ok=True)
    (other / "SKILL.md").write_text("other", encoding="utf-8")
    root = fresh_root(tmp, "foreign")
    root.mkdir(parents=True, exist_ok=True)
    dst = root / src_name
    ins.make_link(dst, other)
    msg = ins.install_one(src_name, src, root, "link", False, True, "verify")
    check("拒绝卸载", msg.startswith("拒绝"), msg)
    check("陌生链接仍在", ins.points_to(dst, other))
    check("other 目录完好", (other / "SKILL.md").is_file())
    check("源仍完好", tree_hash(src) == src_hash_before)

    # ── §6 reparse/points_to 判定自洽 ──────────────────────────────────────
    print("\n§6 链接判定自洽性")
    root = fresh_root(tmp, "judge")
    root.mkdir(parents=True, exist_ok=True)
    dst = root / src_name
    ins.make_link(dst, src)
    check("is_reparse_point(junction)=True", ins.is_reparse_point(dst))
    check("points_to(自身源)=True", ins.points_to(dst, src))
    check("points_to(别的目录)=False", not ins.points_to(dst, other))
    check("真实目录 is_reparse_point=False", not ins.is_reparse_point(root))
    # 关键：is_symlink 与 is_reparse_point 不必一致（junction 在部分 Python 版本上
    # is_symlink() 为 False）——脚本只依赖 is_reparse_point，这条断言把它记录下来。
    print(f"  [INFO] Path.is_symlink()={dst.is_symlink()} "
          f"os.path.islink()={os.path.islink(dst)} "
          f"is_reparse_point()={ins.is_reparse_point(dst)}")

    # ── §7 源已消失的 broken 链接 ─────────────────────────────────────────
    print("\n§7 源消失后的 broken 链接")
    root = fresh_root(tmp, "broken")
    root.mkdir(parents=True, exist_ok=True)
    gone = tmp / "gone-skill"
    gone.mkdir(parents=True, exist_ok=True)
    (gone / "SKILL.md").write_text("gone", encoding="utf-8")
    dst = root / src_name
    ins.make_link(dst, gone)
    shutil.rmtree(gone)                      # 源被删除 → 链接成为孤儿
    check("链接判定仍为 reparse point", ins.is_reparse_point(dst))
    check("points_to(本仓源)=False（已不指向本仓）", not ins.points_to(dst, src))
    msg = ins.install_one(src_name, src, root, "link", False, True, "verify")
    check("拒绝卸载陌生/孤儿链接（保护他人安装）", msg.startswith("拒绝"), msg)
    check("孤儿链接未被误删", ins.is_reparse_point(dst))
    check("真实源仍完好", tree_hash(src) == src_hash_before)
    # 已知缺口（有意保留，已记入 AGENTS.md 待办）：仓库删除某个 skill 后，用户级会
    # 留下孤儿链接；本安装器没有 provenance 元数据，无法安全判定"这个链接是我装的"，
    # 因此宁可不清理（清理错的代价是删掉别人的目录），也不猜。
    print("  [INFO] 孤儿链接需 provenance 元数据才能安全回收（当前有意不清理）")

    # ── 收尾 ───────────────────────────────────────────────────────────────
    print("\n§8 源目录终态复核")
    check("源内容仍逐字节未变", tree_hash(src) == src_hash_before)
    shutil.rmtree(tmp, ignore_errors=True)  # 清临时区（源不在其中）
    check("临时区已清理", not tmp.exists())

    print(f"\n== 结果: {passed} PASS / {len(fails)} FAIL ==")
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
