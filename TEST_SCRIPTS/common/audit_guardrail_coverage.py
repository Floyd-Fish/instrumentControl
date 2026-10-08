"""护栏覆盖性审计（离线）：穷举"手册里的命令"与"各工具白名单"，找误伤（over-block）与漏项。

为什么需要它：2026-09-15 我在改查询护栏时**两次**把合法用法一起拦了（先误拦带参数查询、
再误拦多段回读）——根因都是"只验证了想拦的，没验证不想拦的"。本脚本把这件事自动化：

    §A 黑名单误伤扫描：把六套手册里**每一条命令**喂给 `_is_forbidden`，
       凡被拦的都必须属于复位/存储覆写/远程锁定族（否则就是误伤）；
    §B 查询判据全域验证：每条命令都要能"被查询"——`:CMD?` 与带参数的 `:CMD? <param>`
       必须通过 `_is_query_only`（这就是两次回归的守卫）；
    §C 白名单出出处核对：各工具用于**校验入参**的枚举项必须在对应手册里查得到；
    §D 白名单**双向**比对：手册给出的 item/触发类型枚举 vs 我们的列表，两侧都报
       （手册有我们缺 = **会误拦**；我们有手册无 = 会误放）。

首次运行（2026-09-15）抓到并修掉 3 个真缺陷：
    SDS `MEAS_TYPES` 的 `RISE20T90` → 手册是 `RISE20T80`（打错字：真项被误拦、错项被误放）；
    DHO 触发类型误抄 MHO 的列表（多出 IIS/FLEXray/M1554，其中 M1554 两手册都没有）；
    DHO 测量项缺 `ACRMs`（手册 item 表里有）。

用法：python TEST_SCRIPTS/common/audit_guardrail_coverage.py [--full]     （纯离线）
留痕：TEST_DATA/common/guardrail_coverage_<stamp>.json
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_instruments"))
sys.path.insert(0, str(ROOT / "TEST_SCRIPTS" / "common"))

import server as S  # noqa: E402  护栏本体

FULL = "--full" in sys.argv

MANUALS = {
    "dh1766": "dh1766_control/docs/SCPI_COMMANDS_DH1766A.md",
    "dg832": "dg832_control/docs/02_编程手册.txt",
    "dho": "dho_control/docs/DHO800编程手册_output/DHO800编程手册.md",
    "mho": "mho_control/docs/MHO900编程手册_output/MHO900编程手册.md",
    "sds": "sds_control/docs/SDS800XHD_Series_ProgrammingGuide_CN11G_output/"
           "SDS800XHD_Series_ProgrammingGuide_CN11G.md",
    "sdg": "sdg_control/docs/SDG_Programming-Guide_PG02_C02C_output/"
           "SDG_Programming-Guide_PG02_C02C.md",
    "k3446x": "keysight_3446x/docs/Truevolt_Series_Operating_and_Service_Guide_output/"
              "Truevolt_Series_Operating_and_Service_Guide.md",
}
MANUAL_TEXT = {t: (ROOT / p).read_text(encoding="utf-8", errors="replace")
               for t, p in MANUALS.items()}

# 命中黑名单时，哪些前缀属于"应当拦"的族（其余命中即误伤）
INTENDED_PREFIX = ("*RST", "*SAV", "*RCL", ":SYST:RES", ":SYSTEM:RES",
                   ":SYST:PRES", ":SYSTEM:PRES", ":SYST:FACT", ":SYSTEM:FACT",
                   ":SYST:REM", ":SYSTEM:REM", ":SYST:RWL", ":SYST:LOCK", ":SYSTEM:LOCK",
                   ":SYST:COMM:RLS")

# 提取残留：非 ASCII（中文占位符）、HTML 标签/实体、选项列表语法 —— 都不是真命令
# 注意用**字符类**（含其中任一字符即算残留）；写成字面串 `|"{}` 会漏掉
# `{"OFF"|"CALCulate:DATA"}` 这类选项列表（2026-09-15 实测漏过 2 条）。
_NON_ASCII = re.compile("[^" + chr(0) + "-" + chr(127) + "]")
_ARTIFACT_EXTRA = re.compile("</|<[a-z]+>|&[a-z]+;|[" + re.escape('|"{}') + "]")


def _artifact(cmd: str) -> bool:
    """手册提取残留（HTML 标签 / 中文占位符 / 选项列表语法）——不是真命令，跳过。"""
    return bool(_NON_ASCII.search(cmd) or _ARTIFACT_EXTRA.search(cmd))


def manual_commands(text: str) -> set[str]:
    """从手册文本提取命令写法（与 audit_all_commands 的索引口径一致，但保留原文）。"""
    seg = r"[A-Za-z][A-Za-z0-9]*(?:\{[^{}]*\}|<[^<>]*>|\d+)?"
    out: set[str] = set()
    for m in re.finditer(r":(" + seg + r"(?::" + seg + r")+)\??", text):
        out.add(m.group(1))
    for m in re.finditer(r"(?<![:\w])(" + seg + r"(?::" + seg + r")+)\??", text):
        out.add(m.group(1))
    for m in re.finditer(r":([A-Z][A-Za-z]{2,15})\??(?![\w:])", text):
        out.add(m.group(1))
    return {c for c in out if len(c) >= 4 and not c.endswith("::")}


def param_probe(cmd: str) -> tuple[str, str]:
    """给命令编两个"文档式查询"：`:CMD?` 与 `:CMD? MAX`（用于测查询判据）。

    只取**命令头**（空格前）——手册里很多条目本身是带参数的写命令写法
    （如 `BSWV WVTP,SINE`），整串加 `?` 生成的是伪命令，被拒是正确行为。
    """
    head = cmd.strip().split()[0].rstrip(",")
    return f":{head}?", f":{head}? MAX"


def item_block(text: str, cmd: str, marker: str = "VPP", span: int = 6000) -> set[str]:
    """取命令小节里的"项枚举"：**合并**所有与已知项重合 ≥5 的 `{}` 块。

    为什么要合并而不是"取最大的那块"：提取版在分页处会把同一个枚举切成两块
    （实测 MHO 手册 `:MEASure:ITEM` 的 `FFDelay` 被切到第 217 页的续块里），
    只取一块会得出"手册没有 FFDelay"的假差异（2026-09-15 踩过）。
    """
    m = re.search(r"^#{1,4}[^\n]*" + re.escape(cmd) + r"\b", text, re.M)
    body = text[m.start(): m.start() + span] if m else ""
    known = {"VMAX", "VMIN", "VPP", "VAVG", "VRMS", "VAMP", "PERiod", "FREQuency",
             "RTIMe", "FTIMe", "PWIDth", "PDUTy", "EDGE", "PULSe", "NORMal", "PEAK"}
    merged: set[str] = set()
    for raw in re.findall(r"\{([^{}]{20,2000})\}", body):
        toks = {v.strip() for v in raw.replace("<br>", "").replace("\n", "").split("|") if v.strip()}
        if marker in toks or len(toks & known) >= 5:
            merged |= toks
    return {t for t in merged
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", t)
            and not re.fullmatch(r"(CHANnel\d|D\d+|MATH\d)", t)}


def main() -> int:
    report = {"A_blacklist_false_positive": [], "B_query_rule_reject": [],
              "C_whitelist_missing": [], "D_manual_minus_ours": [], "D_ours_minus_manual": []}
    per_manual = {}

    # ---------------- §A / §B ----------------
    print("=== §A 黑名单误伤扫描 + §B 查询判据全域验证（按手册穷举）===", flush=True)
    for tag, text in MANUAL_TEXT.items():
        cmds = manual_commands(text)
        blocked, rejected, skipped = [], [], 0
        for c in sorted(cmds):
            write_form = c if c.startswith(("*", ":")) else ":" + c
            up = write_form.upper()
            if S._is_forbidden(write_form) and not (
                    up.startswith("*RST") or up.startswith("*SAV") or up.startswith("*RCL")
                    or any(up.startswith(p) for p in INTENDED_PREFIX)):
                blocked.append(write_form)
            if _artifact(c):
                skipped += 1
                continue
            for q in param_probe(c):
                if not S._is_query_only(q):
                    rejected.append(q)
        per_manual[tag] = {"commands": len(cmds), "artifacts_skipped": skipped,
                           "blacklist_false_positive": len(blocked), "query_rejected": len(rejected)}
        report["A_blacklist_false_positive"] += [{"manual": tag, "cmd": c} for c in blocked]
        report["B_query_rule_reject"] += [{"manual": tag, "cmd": c} for c in rejected]
        print(f"  {tag:8s} 命令 {len(cmds):4d} | 提取残留跳过 {skipped:3d} | "
              f"黑名单误伤 {len(blocked)} | 查询式被拒 {len(rejected)}", flush=True)
        if FULL and (blocked or rejected):
            print(f"      误伤: {blocked[:6]} | 被拒: {rejected[:6]}")

    # ---------------- §C 白名单出出处 ----------------
    print("\n=== §C 白名单项是否在对应手册查得到 ===", flush=True)
    import dg832_control
    import mho_control.commands as MC
    import sds_control
    import sds_control.commands as SC
    from rigol_scope import FAMILIES

    checks = {
        "sds_control.MEAS_TYPES": (sds_control.SDS.MEAS_TYPES, "sds"),
        "sds_control.MEAS_ADV_SINGLES": (SC.MEAS_ADV_SINGLES, "sds"),
        "sds_control.MEAS_DUAL_TYPES": (SC.MEAS_DUAL_TYPES, "sds"),
        "mho_control.MEAS_ITEMS_SINGLE": (MC.MEAS_ITEMS_SINGLE, "mho"),
        "mho_control.MEAS_ITEMS_DUAL": (MC.MEAS_ITEMS_DUAL, "mho"),
        "rigol_scope.DHO.measure_items": (FAMILIES["DHO"].measure_items, "dho"),
        "rigol_scope.MHO.measure_items": (FAMILIES["MHO"].measure_items, "mho"),
        "rigol_scope.DHO.trigger_types": (FAMILIES["DHO"].trigger_types, "dho"),
        "rigol_scope.MHO.trigger_types": (FAMILIES["MHO"].trigger_types, "mho"),
        "rigol_scope.DHO.acq_types": (FAMILIES["DHO"].acq_types, "dho"),
        "rigol_scope.MHO.acq_types": (FAMILIES["MHO"].acq_types, "mho"),
        "rigol_scope.DHO.acq_depths": (FAMILIES["DHO"].acq_depths, "dho"),
        "rigol_scope.MHO.acq_depths": (FAMILIES["MHO"].acq_depths, "mho"),
        "rigol_scope.DHO.chan_couplings": (FAMILIES["DHO"].chan_couplings, "dho"),
        "rigol_scope.MHO.chan_bwlimits": (FAMILIES["MHO"].chan_bwlimits or (), "mho"),
        "rigol_scope.MHO.chan_impedances": (FAMILIES["MHO"].chan_impedances or (), "mho"),
        "rigol_scope.DHO.edge_slopes": (FAMILIES["DHO"].edge_slopes, "dho"),
        "rigol_scope.MHO.edge_slopes": (FAMILIES["MHO"].edge_slopes, "mho"),
        "rigol_scope.DHO.wav_formats": (FAMILIES["DHO"].wav_formats, "dho"),
        "rigol_scope.MHO.wav_formats": (FAMILIES["MHO"].wav_formats, "mho"),
        "rigol_scope.DHO.wav_modes": (FAMILIES["DHO"].wav_modes, "dho"),
        "rigol_scope.MHO.wav_modes": (FAMILIES["MHO"].wav_modes, "mho"),
        "rigol_scope.DHO.disp_formats": (FAMILIES["DHO"].disp_formats, "dho"),
        "rigol_scope.MHO.disp_formats": (FAMILIES["MHO"].disp_formats, "mho"),
        "dg832_control.SHAPE_APPL": (tuple(__import__(
            "dg832_control.dg832", fromlist=["SHAPE_APPL"]).SHAPE_APPL.keys()), "dg832"),
    }
    for name, (vals, tag) in checks.items():
        if not vals:
            continue
        text = MANUAL_TEXT[tag].upper()
        missing = [v for v in vals if v and v.upper() not in text]
        if missing:
            report["C_whitelist_missing"].append({"list": name, "manual": tag, "missing": missing})
        print(f"  {name:38s} {len(vals):3d} 项 | 手册查不到: {missing or '无'}", flush=True)

    # ---------------- §D 双向比对 ----------------
    print("\n=== §D 白名单<->手册 双向比对（手册有我们缺 = 会误拦）===", flush=True)
    pairs = [
        ("dho 测量项", ":MEASure:ITEM", "dho",
         set(FAMILIES["DHO"].measure_items) | set(FAMILIES["DHO"].measure_items_dual)),
        ("mho 测量项", ":MEASure:ITEM", "mho",
         set(FAMILIES["MHO"].measure_items) | set(FAMILIES["MHO"].measure_items_dual)),
        ("sds 测量项", ":MEASure:SIMPle:ITEM", "sds", set(sds_control.SDS.MEAS_TYPES)),
        ("dho 触发类型", ":TRIGger:MODE", "dho", set(FAMILIES["DHO"].trigger_types)),
        ("mho 触发类型", ":TRIGger:MODE", "mho", set(FAMILIES["MHO"].trigger_types)),
    ]
    for label, cmd, tag, ours in pairs:
        man = item_block(MANUAL_TEXT[tag], cmd, marker="VPP" if "测量项" in label else "EDGE")
        if not man:
            print(f"  {label:12s} 手册块未摘到（跳过）", flush=True)
            continue
        miss, extra = sorted(man - ours), sorted(ours - man)
        if miss:
            report["D_manual_minus_ours"].append({"list": label, "missing_in_ours": miss})
        if extra:
            report["D_ours_minus_manual"].append({"list": label, "extra_in_ours": extra})
        flag = "OK" if not miss and not extra else "**差异**"
        print(f"  {label:12s} 手册 {len(man):3d} / 我们 {len(ours):3d}  [{flag}]"
              f" 手册有我们缺(会误拦): {miss or '无'} | 我们有手册无(会误放): {extra or '无'}", flush=True)

    # ---------------- 汇总 ----------------
    n_a = len(report["A_blacklist_false_positive"])
    n_b = len(report["B_query_rule_reject"])
    n_d = len(report["D_manual_minus_ours"]) + len(report["D_ours_minus_manual"])
    print("\n=== 汇总 ===")
    print(f"  §A 黑名单误伤 {n_a} 条 / §B 查询式被拒 {n_b} 条 / "
          f"§C 缺出处 {len(report['C_whitelist_missing'])} 组 / §D 双向差异 {n_d} 组")
    for f in report["C_whitelist_missing"]:
        print(f"      §C {f['list']}（{f['manual']}）: {f['missing']}")
    for f in report["D_manual_minus_ours"]:
        print(f"      §D {f['list']} 手册有我们缺（会误拦）: {f['missing_in_ours']}")
    for f in report["D_ours_minus_manual"]:
        print(f"      §D {f['list']} 我们有手册无（会误放）: {f['extra_in_ours']}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / "TEST_DATA" / "common" / f"guardrail_coverage_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"timestamp": datetime.now().isoformat(timespec="seconds"),
                               "per_manual": per_manual, **report},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n留痕: {out}")
    return 0 if (n_a == 0 and n_b == 0 and n_d == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
