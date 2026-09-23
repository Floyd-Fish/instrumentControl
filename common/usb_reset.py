"""USB-TMC 卡死恢复：对指定仪器**重启其 USB PnP 设备**（不用给仪器上下电）。

定位：**故障维护兜底能力**（与 `discovery`/`resolver` 同级的基础设施，在 `common/` 里），
不是某个设备的专属脚本——凡走 USB-TMC 的仪器（DG832/DH1766/USB 示波器…）都适用。
MCP 侧对应工具 `usb_reset`（需 confirm=True）。

背景（真机经验）：USB-TMC 仪器偶发"设备在但会话卡死"——
`*IDN?` 超时 / `VI_ERROR_TMO` / `VI_ERROR_SYSTEM_ERROR`，拔插 USB 能恢复，但拔插需要人到现场。
**等价且更省事的办法：重启该 USB 设备节点（PnP restart）**——USB 重新枚举，设备固件不重启、
设定不丢，通常几秒内即恢复。2026-09-15 DG832 实测过一次 `VI_ERROR_SYSTEM_ERROR`（重连即恢复），
本工具是给"重连也救不回来"那种情况准备的。

⚠ **需要管理员权限**：会弹 UAC（Windows 的既定要求，无法绕过）。默认**只打印将要执行的命令**，
要真执行必须加 `--allow-reset`；提权再单加 `--escalate`（否则提示你手动以管理员运行）。

用法（CLI）：
    # ① 看会做什么（不需要权限，安全）
    python common/usb_reset.py --resource "USB0::0x1AB1::0x0643::<SN>::INSTR" --dry-run

    # ② 真做（非管理员环境会弹 UAC 提权）
    python common/usb_reset.py --resource "<同上>" --allow-reset --escalate --verify-idn

    # ③ 不知道资源串？按 kind 解析（走解析层，不写死地址）
    python common/usb_reset.py --kind dg --dry-run          # kind ∈ sds/sdg/dmm/dho/mho/dg/psu

    # ④ 手动指定 VID/PID（设备已被系统认成别的名字时）
    python common/usb_reset.py --vid 0x1AB1 --pid 0x0643 --serial <SN> --allow-reset

用法（Python）：
    from common.usb_reset import reset_device, find_instance, parse_usb_resource
    ok, out = reset_device(find_instance("1AB1", "0643", "<SN>"))

MCP：工具 `usb_reset(resource|kind, confirm=True, verify_idn=True)`（故障兜底，见 AGENTS.md 铁律#14）。

可选参数：--timeout-s 等待设备回来的秒数（默认 20）；--keep /tmp；--list 只列 USB 仪器设备。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def is_admin() -> bool:
    """当前进程是否有管理员权限（决定 pnputil 能否直接改设备）。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def parse_usb_resource(resource: str) -> tuple[str, str, str] | None:
    """从 VISA 资源串解析 (vid, pid, serial)：USB0::0x1AB1::0x0643::<serial>::INSTR。"""
    m = re.match(r"USB\d*::0x([0-9A-Fa-f]+)::0x([0-9A-Fa-f]+)::([^:]+)::INSTR", resource or "")
    if not m:
        return None
    return m.group(1).upper(), m.group(2).upper(), m.group(3)


def _ps(cmd: str, timeout: int = 30) -> str:
    """跑一条 PowerShell 命令并返回 stdout（PnP 枚举用；不用 shell 拼接外部输入）。"""
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    return (r.stdout or "").strip()


def list_usb_instruments() -> list[dict]:
    """列出本机 PnP 里所有 USB 设备中的仪器（按常见仪器 VID 过滤），返回 [{instance, name, status}]。"""
    # 仪器常见 VID（只列真仪器，避免把 STM32 开发板 0x0483 之类混进来）：
    #   RIGOL 0x1AB1 / Keysight-Agilent 0x0957 / Siglent 0xF4EC / NI 0x3923 / 大华 0x0A69
    vids = ("1AB1", "0957", "F4EC", "3923", "0A69")
    # 注：下面的 '*VID_xx*' 是 PowerShell 通配符，不是 SCPI 命令——
    # 命令审计器会把它误当成 `*VID` 公共命令，已在 verify_audit_extractor.py 基线里登记。
    flt = " -or ".join([f"$_.InstanceId -like '*VID_{v}*'" for v in vids])
    cmd = (f"Get-PnpDevice | Where-Object {{ {flt} }} | "
           "Select-Object -Property InstanceId,Status,FriendlyName | ConvertTo-Json -Compress")
    out = _ps(cmd)
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else [data]


def find_instance(vid: str, pid: str, serial: str | None = None) -> str | None:
    """按 VID/PID（可选序列号）找到 PnP 实例 ID。

    PnP 的 InstanceId 形如 `USB\\VID_1AB1&PID_0643\\<serial>`（序列号在末段，
    也可能被系统改写）——故序列号只作**优先匹配**，找不到就退回 VID/PID 唯一命中。
    """
    pat = f"USB\\\\VID_{vid.upper()}&PID_{pid.upper()}"
    cmd = ("Get-PnpDevice | Where-Object { $_.InstanceId -like '"
           + f"*{vid.upper()}&PID_{pid.upper()}*"
           + "' } | Select-Object -ExpandProperty InstanceId")
    out = _ps(cmd)
    ids = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not ids:
        return None
    if serial:
        for i in ids:
            if serial.lower() in i.lower():
                return i
    return ids[0]


def restart_device(instance: str, timeout_s: int = 60) -> tuple[bool, str]:
    """用 `pnputil /restart-device` 重启设备节点（比 禁用+启用 更安全：一步完成、无中间态）。"""
    r = subprocess.run(["pnputil", "/restart-device", instance],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout_s)
    ok = r.returncode == 0
    return ok, ((r.stdout or "") + (r.stderr or "")).strip()[:400]


def wait_back(resource: str, timeout_s: int = 20) -> tuple[bool, str]:
    """等设备回来：反复尝试 *IDN?（VISA open 会随枚举完成而成功）。"""
    from common.discovery import identify
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        idn = identify(resource, timeout_ms=2000)
        if idn:
            return True, idn
        time.sleep(1.0)
    return False, f"{timeout_s}s 内未回来"


def escalate_and_run(args_list: list[str], result_file: Path) -> int:
    """以管理员身份重跑本脚本（弹 UAC），子进程把结果写到 result_file。

    用 PowerShell 的 Start-Process -Verb RunAs —— 这是 Windows 上唯一的提权入口，
    必然弹 UAC 对话框（用户可取消；取消则什么都不做，安全）。
    """
    py = sys.executable
    argv = [str(Path(__file__).resolve()), *args_list, "--result-json", str(result_file)]
    quoted = ",".join("'" + a.replace("'", "''") + "'" for a in argv)
    ps = (f"$p = Start-Process -FilePath '{py}' -Verb RunAs -Wait -PassThru "
          f"-ArgumentList @({quoted}); exit $p.ExitCode")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 and (r.stderr or "").strip():
        print("（提权失败或被取消）", (r.stderr or "").strip()[:200])
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser(
        description="USB-TMC 卡死恢复：重启仪器的 USB PnP 设备（需管理员权限；非管理员加 --escalate 弹 UAC）")
    ap.add_argument("--resource", help="完整 VISA 资源串（USB0::0xVVVV::0xPPPP::SN::INSTR）")
    ap.add_argument("--kind", help="或给解析层的设备类（如 dg/dho/mho/psu），自动 resolve 出资源串")
    ap.add_argument("--vid"), ap.add_argument("--pid"), ap.add_argument("--serial")
    ap.add_argument("--list", action="store_true", help="只列出本机 USB 仪器设备")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的命令（默认行为，无需权限）")
    ap.add_argument("--allow-reset", action="store_true", help="确认执行重启（必须显式给）")
    ap.add_argument("--escalate", action="store_true", help="无管理员权限时弹 UAC 提权重跑")
    ap.add_argument("--verify-idn", action="store_true", help="重置后跑 *IDN? 验证设备真的回来了")
    ap.add_argument("--timeout-s", type=int, default=20, help="等待设备回来的秒数（默认 20）")
    ap.add_argument("--result-json", help="（内部用）提权子进程写结果的文件")
    a = ap.parse_args()

    if a.list:
        devs = list_usb_instruments()
        print(json.dumps(devs or "（未发现 USB 仪器设备）", ensure_ascii=False, indent=2))
        return 0

    # ① 定位资源串 → VID/PID/序列号
    res = a.resource
    if not res and a.kind:
        from common.resolver import resolve
        res = resolve(a.kind)
    vid, pid, serial = (a.vid, a.pid, a.serial)
    if res:
        parsed = parse_usb_resource(res)
        if not parsed:
            print(f"× 该资源串不是 USB-TMC（本工具只处理 USB）：{res}\n"
                  f"  LAN 卡死请重连/换协议（inst0 ↔ raw socket），不是本工具的场景。")
            return 2
        v, p, s = parsed
        vid, pid, serial = vid or v, pid or p, serial or s
        print(f"资源串：{res}\n  → VID=0x{vid} PID=0x{pid} SERIAL={serial}")
    if not (vid and pid):
        print("× 需要 --resource / --kind / --vid+--pid 之一")
        return 2

    # ② 定位 PnP 实例
    inst = find_instance(vid, pid, serial)
    if not inst:
        print(f"× 未找到 VID=0x{vid} PID=0x{pid} 的 PnP 设备。"
              f"设备可能已掉线（需拔插）或被识别成其它名字——可先 --list 看看。")
        return 3
    print(f"PnP 实例：{inst}")

    cmdline = f'pnputil /restart-device "{inst}"'
    print(f"将执行：{cmdline}   （USB 重新枚举；仪器固件不重启、设定不丢）")

    if a.dry_run or not a.allow_reset:
        print("\n（默认只打印。真执行请加 --allow-reset；无管理员权限时再加 --escalate 弹 UAC）")
        if not is_admin():
            print("  当前进程无管理员权限 → 直接执行会被系统拒绝。两种办法：")
            print("    ① 本脚本加 --escalate（自动弹 UAC 提权重跑）")
            print("    ② 手动：管理员 PowerShell 里执行 "
                  f'pnputil /restart-device "{inst}"')
        else:
            print("  （当前进程已是管理员，加 --allow-reset 即可直接执行）")
        return 0

    # ③ 权限检查（不足则提权）
    if not is_admin():
        if not a.escalate:
            print("× 需要管理员权限（pnputil 改设备）。加 --escalate 会自动弹 UAC，"
                  "或以管理员身份重开终端再跑。")
            return 4
        print("→ 无管理员权限，弹 UAC 提权重跑本脚本…")
        child_args = ["--vid", vid, "--pid", pid, "--allow-reset", "--verify-idn",
                      "--timeout-s", str(a.timeout_s)]
        if serial:
            child_args += ["--serial", serial]
        if res:
            child_args = ["--resource", res, "--allow-reset", "--verify-idn",
                          "--timeout-s", str(a.timeout_s)]
        rf = Path(a.result_json or (Path(ROOT) / "TEST_DATA" / "common" / "usb_reset_result.json"))
        rf.parent.mkdir(parents=True, exist_ok=True)
        rc = escalate_and_run(child_args, rf)
        print(f"提权子进程退出码 {rc}")
        if rf.exists():
            print("结果：", rf.read_text(encoding="utf-8")[:600])
        return rc

    # ④ 执行 + 验证
    ok, out = restart_device(inst)
    print(("✓ " if ok else "× ") + f"restart-device 返回 {ok}：{out}")
    verified, note = (None, "")
    if ok and res and a.verify_idn:
        verified, note = wait_back(res, a.timeout_s)
        print(("✓ " if verified else "× ") + f"设备回来：{note}")
    result = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "resource": res,
              "instance": inst, "vid": vid, "pid": pid, "serial": serial,
              "restart_ok": ok, "output": out, "verified_idn": verified, "note": note}
    if a.result_json:
        Path(a.result_json).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"留痕: {a.result_json}")
    return 0 if (ok and verified is not False) else 1


if __name__ == "__main__":
    sys.exit(main())
