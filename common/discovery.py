"""VISA 设备统一发现引擎：显式指定 → 已有资源列表 → 网段扫描（fallback 可开关）。

查找链（find_device）：
    层① resource 显式资源串（USB/TCPIP 均可）
    层② hosts 显式 host/IP 列表 → TCPIP0::<host>::<proto>::INSTR
    层③ list_resources 全扫（传统行为）
    层④ CIDR 网段并发扫描
层③④ 仅在未给显式参数、或显式全失败且 allow_scan=True 时执行。

安全语义：
    - 显式指定在线但 *IDN? 不匹配 idn_contains → ValueError（不静默换设备）；
    - 显式指定无响应 → allow_scan=True 才降级下一层；
    - 每层尝试均记录，最终失败 RuntimeError 打印完整链路可回溯。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import pyvisa

SCAN_WORKERS = 32
SCAN_TIMEOUT_MS = 1500
# TCP 端口预筛（任一通即候选）：VXI-11 portmapper / HiSLIP / SCPI raw(5025 Keysight 等 /
# 5555 Rigol)。仅用于快速淘汰死地址与非仪器主机；设备身份仍由 VISA + *IDN? 确认。
SCAN_PROBE_PORTS = (111, 4880, 5025, 5555)
PROBE_TIMEOUT_S = 0.6
PROBE_WORKERS = 128
# 端口预筛并发上限：不限并发会打满本机 TCP 栈导致**假阴性**（实测 1016 地址
# 无限并发时连已知在线的 4 台仪器全部漏检；限流 256 后 4/4 命中）。
PROBE_ALIVE_CONCURRENCY = 256
# 大批量探测的**批大小**：一次性 gather 整个 /16（6.5 万协程）会 MemoryError，
# 分块后峰值内存与块大小成正比（2026-09-16 实机踩到，见 probe_open_ports）。
PROBE_CHUNK = 2048

# 串口探测的超时（2026-09-23 两轮收紧：8.0 → 2.0；内层 500ms）。
#
# 取值依据（合并双方 2026-09-23 各自实测后收敛到同一组值）：
# * 外层（hard_timeout_s，连子进程一起杀）：只该覆盖"子进程正常走完一趟"的开销
#   —— 解释器启动（Windows 约 0.3~0.5s）+ pyvisa import + open + `*IDN?` query，
#   实测正常口约 1.4s；挂起口靠子进程 kill 收场，不靠这个上限变成成功。旧值 8s
#   只是让每个挂起口白等（本机 5 个挂起口 × 8s ÷ 4 workers ≈ 10s）。
# * 内层（open_timeout / VISA timeout 500ms）：`*IDN?` 响应最长约 100 字节，按
#   **最慢 4800 baud** 也只有 100×10/4800 ≈ 0.21s，500ms 余量充足。
# 两个值都可用环境变量覆盖（接真实串口仪器且响应慢时调大）。
SERIAL_PROBE_TIMEOUT_S = 2.0
SERIAL_PROBE_OPEN_TIMEOUT_MS = 500
SERIAL_PROBE_VISA_TIMEOUT_MS = 500

# 本进程内探测挂起的串口：后续发现跳过，避免每轮重复空等（子进程隔离已保证
# 不会污染本进程，这里纯粹是速度优化）。显式重新发现用 forget_hanging_ports() 清空。
_HANGING_PORTS: set[str] = set()


def forget_hanging_ports() -> None:
    """清空「挂起串口」缓存（显式重新发现前调用，允许重新探测这些口）。"""
    _HANGING_PORTS.clear()

# 串口探测走**子进程**（不是本进程线程）。
# 为什么必须子进程（2026-09-15 实测，MCP 服务器反复掉线）：
#   某些串口会让驱动层 open **永久挂起**且无视 open_timeout。在本进程里用
#   daemon 线程 + join 超时"看起来"能脱身，但那个线程仍卡在驱动里、持续持有
#   VISA 原生状态；此后**本进程的任何 VISA 调用都会让进程直接死亡**（无异常、
#   无栈，客户端只看到连接断开）。实测：设备工具因地址缓存过期触发自动发现，
#   第一次 37s 返回后，第二个工具调用即整个服务器掉线；同一序列在修复前后
#   稳定复现。
#   子进程隔离后，超时即 kill——子进程连同卡住的句柄一起消失，本进程原生
#   状态不被污染。代价是每口一次解释器启动（并行，约 1s 量级），可接受。
_SERIAL_PROBE_SCRIPT = r'''
import json, sys
import pyvisa
res, timeout_ms, open_ms = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
out = {"resource": res, "idn": None, "note": None}
rm = None
try:
    rm = pyvisa.ResourceManager()
    inst = rm.open_resource(res, open_timeout=open_ms)
    inst.timeout = timeout_ms
    inst.write_termination = "\n"
    inst.read_termination = "\n"
    try:
        idn = inst.query("*IDN?").strip()
        out["idn"] = idn or None
        if not idn:
            out["note"] = "打开成功但无 *IDN? 响应（非 SCPI 设备或波特率不匹配）"
    finally:
        inst.close()
except Exception as e:
    code = getattr(e, "error_code", 0)
    if "BUSY" in str(e).upper() or code == -1073807346:
        out["note"] = "串口被占用（其他程序打开中）"
    else:
        out["note"] = "打开失败: " + type(e).__name__
finally:
    if rm is not None:
        try:
            rm.close()
        except Exception:
            pass
print(json.dumps(out, ensure_ascii=False))
'''


def probe_serial_isolated(resource: str, timeout_ms: int = SERIAL_PROBE_VISA_TIMEOUT_MS,
                          hard_timeout_s: float = SERIAL_PROBE_TIMEOUT_S,
                          open_timeout_ms: int = SERIAL_PROBE_OPEN_TIMEOUT_MS) -> dict:
    """在**子进程**里探测一个串口，超时即杀。返回 {resource,idn,note}。

    这是唯一安全的串口探测方式——详见 `_SERIAL_PROBE_SCRIPT` 上方说明
    （本进程线程探测会留下卡死线程，导致后续任何 VISA 调用令进程崩溃）。
    """
    cmd = [sys.executable, "-c", _SERIAL_PROBE_SCRIPT, resource,
           str(int(timeout_ms)), str(int(open_timeout_ms))]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=hard_timeout_s,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"resource": resource, "idn": None,
                "note": f"探测超时({hard_timeout_s:.0f}s，驱动挂起，疑似被占用)"}
    except Exception as e:
        return {"resource": resource, "idn": None,
                "note": f"子进程启动失败: {type(e).__name__}"}
    if p.returncode != 0:
        return {"resource": resource, "idn": None,
                "note": f"子进程异常退出 rc={p.returncode}"}
    for line in reversed((p.stdout or "").strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"resource": resource, "idn": None, "note": "子进程无有效输出"}


def probe_serials_isolated(resources: list[str], timeout_ms: int = SERIAL_PROBE_VISA_TIMEOUT_MS,
                           hard_timeout_s: float = SERIAL_PROBE_TIMEOUT_S,
                           workers: int = 4) -> list[dict]:
    """并行探测多个串口（每个一口子进程），保持入参顺序返回。

    已确认挂起的口（`_HANGING_PORTS`）直接跳过返回占位结果——省下每轮的空等
    （实测 9 口中 4 个挂起，全量 55s → 跳过挂起口后约 10s）。子进程隔离
    已保证挂起口不会污染本进程，缓存只是加速；`forget_hanging_ports()` 可清空。
    """
    if not resources:
        return []
    todo = [r for r in resources if r not in _HANGING_PORTS]
    skipped = [r for r in resources if r in _HANGING_PORTS]
    results: dict[str, dict] = {
        r: {"resource": r, "idn": None, "note": "已知挂起（本轮跳过，instr_discover 可重探）"}
        for r in skipped
    }
    if todo:
        with ThreadPoolExecutor(max_workers=min(workers, len(todo)) or 1) as pool:
            for entry in pool.map(
                    lambda r: probe_serial_isolated(r, timeout_ms, hard_timeout_s), todo):
                results[entry["resource"]] = entry
                if str(entry.get("note") or "").startswith("探测超时"):
                    _HANGING_PORTS.add(entry["resource"])
    return [results[r] for r in resources]


@dataclass
class FindResult:
    """发现结果：resource 命中的资源串，idn 为 *IDN? 原文，source 标记命中层。"""

    resource: str
    idn: str
    source: str  # explicit | listed | scanned


def list_resources() -> list[str]:
    """列出本机所有 VISA 资源（USB/串口/LAN...）。"""
    rm = pyvisa.ResourceManager()
    try:
        return list(rm.list_resources())
    finally:
        rm.close()


def identify(resource: str, timeout_ms: int = 3000, rm: "pyvisa.ResourceManager | None" = None) -> Optional[str]:
    """对单个资源发送 *IDN?，成功返回识别串，失败/超时返回 None。

    SOCKET 资源自动配置 \\n 读写终止符（VISA socket 会话无协议层终止符，
    不配则命令不完整导致设备不应答）。open_timeout 与读超时同设——
    否则离线资源的 TCP 连接阶段可达 60s+（系统默认）。

    `rm`：可选，传入**调用方共享的** ResourceManager（探测多个资源时强烈建议）。
    实测（2026-09-15）：多线程各自 `ResourceManager()` 并发 open 会随机抛
    `VI_ERROR_INV_OBJECT`（进程内 VISA 运行时初始化竞争）——`instr_discover`
    的并行探测因此间歇性整轮失败。共享单例 + 串行/低并发可消除；不传则保持
    原行为（自己建、自己关）。
    """
    kwargs: dict = {}
    if "SOCKET" in resource.upper():
        kwargs = {"read_termination": "\n", "write_termination": "\n"}
    own = rm is None
    if own:
        rm = pyvisa.ResourceManager()
    try:
        inst = rm.open_resource(resource, open_timeout=timeout_ms, **kwargs)
        inst.timeout = timeout_ms
        try:
            return inst.query("*IDN?").strip()
        finally:
            inst.close()
    except Exception:
        return None
    finally:
        if own:
            rm.close()


def identify_all(resources: list[str], timeout_ms: int = 3000,
                 workers: int = 4) -> dict[str, Optional[str]]:
    """批量识别资源，返回 {resource: idn 或 None}。

    共享**单个** ResourceManager（并发各自建 RM 会随机 `VI_ERROR_INV_OBJECT`，
    见 `identify` 说明）。默认 4 线程——低并发既快又稳（VISA 运行时对并发
    open 敏感，实测 16 线程必挂、8 偶挂、4 稳定）。
    """
    if not resources:
        return {}
    rm = pyvisa.ResourceManager()
    try:
        if len(resources) == 1 or workers <= 1:
            return {r: identify(r, timeout_ms, rm) for r in resources}
        with ThreadPoolExecutor(max_workers=min(workers, len(resources))) as pool:
            out = list(pool.map(lambda r: identify(r, timeout_ms, rm), resources))
        return dict(zip(resources, out))
    finally:
        try:
            rm.close()
        except Exception:
            pass


def scan() -> dict[str, Optional[str]]:
    """扫描全部已有 VISA 资源，返回 {resource: idn}，无法识别的设备为 None。"""
    result: dict[str, Optional[str]] = {}
    for res in list_resources():
        result[res] = identify(res)
    return result


def tcpip_resource(host: str, proto: str = "inst0") -> str:
    """host 或完整资源串规范化为 TCPIP 资源名；已含 '::' 视为完整资源串原样返回。"""
    if "::" in host:
        return host
    return f"TCPIP0::{host}::{proto}::INSTR"


# LAN 多协议探测顺序（实测 2026-08-23：RIGOL DHO 系列示波器仅 raw socket 5555 可达；
# 大华 DH1766A-1 电源仅 raw socket 5025 可达；VISA SOCKET 会话必须显式配置
# \n 终止符，否则命令不完整导致超时）
LAN_PROTOCOLS: tuple[tuple[str, dict], ...] = (
    ("TCPIP0::{host}::inst0::INSTR", {}),
    ("TCPIP0::{host}::hislip0::INSTR", {"read_termination": "\n"}),
    ("TCPIP0::{host}::5025::SOCKET",
     {"read_termination": "\n", "write_termination": "\n"}),
    ("TCPIP0::{host}::5555::SOCKET",
     {"read_termination": "\n", "write_termination": "\n"}),
)

# 各协议对应的「预筛端口」，顺序与 LAN_PROTOCOLS 一一对应。
# 识别阶段据此**只试端口开着的协议**，避免对每个候选把 4 个协议挨个超时试一遍
# （实测这是 LAN 识别阶段 23s 的主因）：inst0(VXI-11) 走 portmapper 111、
# hislip0 走 4880、后两条是 SCPI raw socket（5025 / 5555）。
LAN_PROTOCOL_PORTS: tuple[int, ...] = (111, 4880, 5025, 5555)


def identify_lan(host: str, timeout_ms: int = 3000,
                 rm: "pyvisa.ResourceManager | None" = None,
                 open_ports: "set[int] | None" = None) -> Optional[tuple[str, str]]:
    """按 LAN_PROTOCOLS 逐协议探测 *IDN?，成功返回 (可用资源串, idn)，全败返回 None。

    host 含 '::' 视为完整资源串，仅按其本身探测。

    `rm`：可选，传入**调用方共享的** ResourceManager（并发探测多个地址时建议）。
    实测（2026-09-15）：多线程各自 `ResourceManager()` 并发 open 会随机抛
    `VI_ERROR_INV_OBJECT`，且该异常会从 `rm.close()` 里抛出、冲垮整轮扫描——
    `instr_discover` 的 LAN 段因此可能整个失败。共享单例可消除。
    """
    if "::" in host:
        resources = [host]
    else:
        pairs = list(zip(LAN_PROTOCOLS, LAN_PROTOCOL_PORTS))
        if open_ports is not None:
            # 预筛已探明端口：只试端口开着的协议（开着才可能应答），
            # 保留其余作为兜底——端口探测可能因限流/防火墙漏报。
            picked = [p for p in pairs if p[1] in open_ports]
            resources = [t.format(host=host) for (t, _), _p in picked] or                         [t.format(host=host) for t, _ in LAN_PROTOCOLS]
        else:
            resources = [t.format(host=host) for t, _ in LAN_PROTOCOLS]
    own = rm is None
    if own:
        rm = pyvisa.ResourceManager()
    try:
        for res in resources:
            kwargs = next(
                (kw for t, kw in LAN_PROTOCOLS if t.format(host=host) == res),
                {} if res.endswith("INSTR") else
                {"read_termination": "\n", "write_termination": "\n"},
            )
            try:
                inst = rm.open_resource(res, open_timeout=timeout_ms, **kwargs)
                inst.timeout = timeout_ms
                try:
                    idn = inst.query("*IDN?").strip()
                    if idn:
                        return (res, idn)
                finally:
                    inst.close()
            except Exception:
                continue
        return None
    finally:
        if own:
            try:
                rm.close()
            except Exception:   # 会话已失效时 close 仍可能抛，不能让它冲垮调用方
                pass


def identify_lan_all(hosts: list[str], timeout_ms: int = 3000,
                     workers: int = 32,
                     open_ports: "dict[str, set[int]] | None" = None) -> dict[str, Optional[tuple[str, str]]]:
    """批量识别 LAN 地址，返回 {host: (资源串, idn) 或 None}。

    共享单个 ResourceManager——并发各自建 RM 会随机 `VI_ERROR_INV_OBJECT`
    并冲垮整轮 LAN 扫描（见 `identify_lan` 说明）。
    """
    if not hosts:
        return {}
    rm = pyvisa.ResourceManager()
    try:
        with ThreadPoolExecutor(max_workers=min(workers, len(hosts))) as pool:
            out = list(pool.map(
                lambda h: identify_lan(h, timeout_ms, rm,
                                       (open_ports or {}).get(h)),
                hosts))
        return dict(zip(hosts, out))
    finally:
        try:
            rm.close()
        except Exception:
            pass


def _is_scannable(addr: "ipaddress.IPv4Address") -> bool:
    """该地址是否值得当作「仪器可能在的网段」。

    接受 RFC1918 私网；显式排除 198.18.0.0/15（benchmark 段，代理 fake-IP 用——
    VISA open 会挂起且地址全是假的）、以及 169.254/16（APIPA 自动配置，无路由）。
    """
    if addr in ipaddress.ip_network("198.18.0.0/15"):
        return False
    if addr in ipaddress.ip_network("169.254.0.0/16"):
        return False
    return (addr in ipaddress.ip_network("10.0.0.0/8")
            or addr in ipaddress.ip_network("172.16.0.0/12")
            or addr in ipaddress.ip_network("192.168.0.0/16"))


def local_cidrs(max_prefix: int = 16) -> list[str]:
    """本机所有接口所在的私网网段（**按接口真实掩码**，按接口顺序去重，可能多个）。

    为什么不用「连 8.8.8.8 看出口 IP」（`detect_cidr` 的做法）：仪器可达与否
    取决于**本机是否有该网段的接口地址**，与「默认路由指向哪」是两回事。实测
    （2026-09-15）本机同时挂着 4 个私网段（公司网 10.165/另一个 WiFi 192.168.2/
    仪器网段 192.168.31/ZeroTier 172.29）——出口 IP 只是其中一张网卡，默认路由
    指向别的网段时会**静默扫错网段并返回空结果**（看起来像"所有设备都离线"）。

    代理 TUN 模式下这个方法尤其重要：TUN 只加虚拟网卡 + 改默认路由，**不会删掉
    物理网卡的地址**——所以仪器网段仍在列表里，照常可扫；而出口 IP 会变成
    198.18.0.1（fake-IP），旧的 `detect_cidr` 会直接放弃扫描。

    ⚠ **必须用接口真实掩码，不能假设 /24**（2026-09-16 实机踩到）：本机仪器网卡
    是 `192.168.1.100/16`（掩码 255.255.0.0），而仪器可能在 `192.168.31.x`——
    按 /24 算会把同一广播域里但不在 `/24` 内的仪器**静默漏掉**（扫描看起来正常、
    结果却是"没有设备"，最难查的一种假阴性）。`max_prefix` 给网段大小兜底：
    掩码比它更大（网段更宽）时按它收敛（默认 /16 = 最多 65534 台，保护扫描成本），
    并在返回值里仍是**真实可扫范围与接口一致**的网段。

    依赖 psutil；不可用时退回 `detect_cidr()` 的单网段结果（行为不劣化）。
    """
    seen: list[str] = []
    try:
        import psutil
        for _name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family != socket.AF_INET or not a.address:
                    continue
                try:
                    ip = ipaddress.ip_address(a.address)
                except ValueError:
                    continue
                if not _is_scannable(ip):
                    continue
                prefix = _netmask_to_prefix(getattr(a, "netmask", None)) or 24
                prefix = max(prefix, max_prefix)          # 太宽的网段按 max_prefix 收敛
                net = str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))
                if net not in seen:
                    seen.append(net)
    except Exception:
        pass
    if not seen:
        single = detect_cidr()
        if single:
            seen.append(single)
    return seen


def excluded_cidrs() -> list[str]:
    """用户配置的「不要扫」网段（**只影响扫描范围，不影响地址解析**）。

    来源（后者补充前者）：
      * 环境变量 `INSTRUMENT_EXCLUDE_CIDRS`（逗号/分号分隔）
      * 配置目录 `devices.json` 的 `exclude_cidrs` 数组

    动机（2026-09-23 实测）：本机同时挂着公司网与仪器网，`local_cidrs()` 会返回
    两个 /16（合计 131,830 台主机），而仪器根本不在公司网那一侧——每轮白扫数分钟，
    期间还占着执行器 BUSY。排除后扫描范围只留真正可能有仪器的网段。

    已配置（devices.json）与已缓存（last_good_resources.json）的地址**不受影响**：
    排除只作用在"自动扫描"这一步。
    """
    out: list[str] = []
    raw = os.environ.get("INSTRUMENT_EXCLUDE_CIDRS", "")
    for part in re.split(r"[,;]", raw):
        part = part.strip()
        if part:
            out.append(part)
    try:
        from .resolver import CONFIG_DIR  # noqa: PLC0415  （同包，避免import期循环）
        cfg = _load_json_quiet(Path(CONFIG_DIR) / "devices.json")
        for part in (cfg.get("exclude_cidrs") or []):
            if isinstance(part, str) and part.strip():
                out.append(part.strip())
    except Exception:
        pass
    seen: list[str] = []
    for c in out:
        try:
            norm = str(ipaddress.ip_network(c, strict=False))
        except ValueError:
            continue                                  # 写错的项忽略，不让它拖垮扫描
        if norm not in seen:
            seen.append(norm)
    return seen


def _load_json_quiet(path: "Path") -> dict:
    """读 JSON，任何异常都返回空 dict（配置读不到不该影响扫描）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def local_scan_segments(cidrs: Optional[list[str]] = None) -> list[str]:
    """把 `local_cidrs()` 的结果排成**由窄到宽**的扫描顺序（近邻 /24 优先）。

    为什么要这一步（2026-09-16 设计复核）：`local_cidrs()` 现在按接口**真实掩码**
    返回（本机仪器网卡是 /16）——直接扫 /16 要 ~166s/4 端口，而绝大多数情况下仪器
    就在与 PC **同一个 /24** 里。先扫近邻 /24（~5s）命中即返回，未命中才放宽到
    整个网段，兼顾"常见情形快"与"宽网段不漏"。显式传入 `cidrs` 时原样返回
    （调用方自己指定了范围，不要替他改）。

    另外**剔除 `excluded_cidrs()` 里配置的网段**（2026-09-23 加）：公司网/Tailscale
    那一侧不会有仪器，扫它纯属浪费（本机因此每轮白扫两个 /16）。
    """
    nets = list(cidrs) if cidrs else local_cidrs()
    excl: list[ipaddress.IPv4Network] = []
    for c in excluded_cidrs():
        try:
            excl.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            continue

    def _excluded(net_str: str) -> bool:
        try:
            n = ipaddress.ip_network(net_str, strict=False)
        except ValueError:
            return False
        return any(n.subnet_of(e) for e in excl)

    out: list[str] = []
    for net in nets:
        try:
            network = ipaddress.ip_network(net, strict=False)
        except ValueError:
            continue
        if network.prefixlen >= 24:
            if net not in out and not _excluded(net):
                out.append(net)
            continue
        # 宽网段：把它包含的"接口所在 /24"排前面（用接口地址定位，而不是猜第一个 /24）
        narrows = [str(n) for n in _interface_slash24s() if n.subnet_of(network)]
        for n in narrows:
            if n not in out and not _excluded(n):
                out.append(n)
        if net not in out and not _excluded(net):    # 兜底：宽网段本身排最后
            out.append(net)
    return out


def _interface_slash24s() -> list["ipaddress.IPv4Network"]:
    """本机接口地址各自所在的 /24（psutil 不可用时返回空列表）。"""
    nets: list["ipaddress.IPv4Network"] = []
    try:
        import psutil
        for _name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family != socket.AF_INET or not a.address:
                    continue
                try:
                    ip = ipaddress.ip_address(a.address)
                except ValueError:
                    continue
                if not _is_scannable(ip):
                    continue
                net = ipaddress.ip_network(f"{ip}/24", strict=False)
                if net not in nets:
                    nets.append(net)
    except Exception:
        pass
    return nets


def _netmask_to_prefix(netmask: Optional[str]) -> Optional[int]:
    """把 `255.255.0.0` 这类掩码转成 prefix（16）；非法/缺失返回 None。"""
    if not netmask:
        return None
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
    except Exception:
        return None


def proxy_likely() -> bool:
    """本机是否疑似在跑代理/VPN（用于把"扫不到"解释成可操作的建议）。

    只看两件事：环境变量里有 proxy 配置，或存在 198.18.0.0/15 的接口地址
    （TUN fake-IP 的典型特征）。不追求完备——目的是让报错文案能对症下药。
    """
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                "https_proxy", "all_proxy"):
        if os.environ.get(key):
            return True
    try:
        import psutil
        for _name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and a.address:
                    try:
                        if ipaddress.ip_address(a.address) in ipaddress.ip_network("198.18.0.0/15"):
                            return True
                    except ValueError:
                        pass
    except Exception:
        pass
    return False


def detect_cidr() -> Optional[str]:
    """经 UDP 路由探测本机出口 IP，返回所在 /24 网段；失败返回 None。

    仅接受 RFC1918 私网（10/172.16-31/192.168）。注意 Python 的 is_private
    会把 198.18.0.0/15（benchmark 段，代理 fake-IP 常用）也判为私有，
    必须显式排除——扫它得到 254 个假地址且 VISA open 全部挂起。

    ⚠ 这是「默认路由那张网卡」的网段，多网卡/代理开启时会选错或返回 None。
    需要"仪器可能在哪几个网段"请用 `local_cidrs()`（发现层用的是它）。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        addr = ipaddress.ip_address(ip)
        if not _is_scannable(addr):
            return None
        return str(ipaddress.ip_network(f"{ip}/24", strict=False))
    except Exception:
        return None
    finally:
        s.close()


def probe_alive(ip: str, ports: tuple[int, ...] = SCAN_PROBE_PORTS,
                timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    """TCP 端口预筛：任一指定端口可建立连接返回 True（死地址秒级淘汰）。"""
    for port in ports:
        try:
            with socket.create_connection((ip, port), timeout=timeout_s):
                return True
        except OSError:
            continue
    return False


async def _probe_alive_async(ip: str, ports: tuple[int, ...], timeout_s: float) -> bool:
    """单个地址：**所有端口并发**探测，任一通即 True。

    为什么并发：对不存在的主机，TCP SYN 无响应要等满超时——端口串行时一个地址
    最坏 sum(ports)×timeout（实测 4×0.6=2.4s，1016 个地址要 19.5s）；并发后
    降到 max(timeout)（约 0.6s），整轮预筛 19.5s → ~5s。
    """
    async def one(port: int) -> bool:
        try:
            fut = asyncio.open_connection(ip, port)
            reader, writer = await asyncio.wait_for(fut, timeout=timeout_s)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            return False

    results = await asyncio.gather(*(one(p) for p in ports), return_exceptions=True)
    return any(r is True for r in results)


def chunked(seq: list, size: int) -> "list[list]":
    """把列表切成固定大小的块（大网段分批用；`size<=0` 视为不分块）。"""
    if size and size > 0:
        return [seq[i:i + size] for i in range(0, len(seq), size)]
    return [seq]


def probe_open_ports(ips: list[str], ports: tuple[int, ...] = SCAN_PROBE_PORTS,
                     timeout_s: float = PROBE_TIMEOUT_S,
                     concurrency: int = PROBE_ALIVE_CONCURRENCY,
                     chunk: int = PROBE_CHUNK) -> dict[str, set[int]]:
    """批量探测各地址**开放了哪些**预筛端口，返回 {ip: {port,...}}（只含通了的）。

    比 `probe_alive_many` 多返回端口明细——识别阶段据此**只试对应协议**，避免对
    每个候选把 4 个协议挨个超时试一遍（实测识别阶段 23s 主要就是这些空等）。

    **内部按 `chunk` 分批**（2026-09-16 实机踩到）：一次性 `gather` 整个 /16 的
    6.5 万个协程会直接 `MemoryError`（每协程 + 连接对象都是真内存）。分块后
    峰值内存与块大小成正比，且实测 /16 单端口约 44s（800 并发）。
    """
    if not ips:
        return {}
    sem = asyncio.Semaphore(max(1, concurrency))

    async def guarded(ip: str) -> tuple[str, set[int]]:
        async with sem:
            async def one(port: int) -> tuple[int, bool]:
                try:
                    r, w = await asyncio.wait_for(asyncio.open_connection(ip, port),
                                                  timeout=timeout_s)
                    w.close()
                    try:
                        await w.wait_closed()
                    except Exception:
                        pass
                    return port, True
                except Exception:
                    return port, False
            pairs = await asyncio.gather(*(one(p) for p in ports))
            return ip, {p for p, ok in pairs if ok}

    async def run_all() -> dict[str, set[int]]:
        out: dict[str, set[int]] = {}
        for part in chunked(ips, chunk):
            out.update(dict(await asyncio.gather(*(guarded(ip) for ip in part))))
        return out

    try:
        return asyncio.run(run_all())
    except RuntimeError:
        return {ip: {p for p in ports if probe_alive(ip, (p,), timeout_s)} for ip in ips}


def probe_alive_many(ips: list[str], ports: tuple[int, ...] = SCAN_PROBE_PORTS,
                     timeout_s: float = PROBE_TIMEOUT_S,
                     concurrency: int = PROBE_ALIVE_CONCURRENCY) -> list[bool]:
    """批量 TCP 端口预筛（asyncio 并发 + **限流**），返回与 `ips` 等长的存活列表。

    ⚠ 必须限流：实测（2026-09-16，本机 1016 地址）不限并发时 4000+ 连接同时发起会
    打满本机 TCP 栈 → 已知在线的 4 台仪器**全部漏检**（alive=0，0.9s 就"扫完"，看着
    很快其实全是假阴性）；限流 256 后 4/4 命中。宁可慢几秒，不可漏报设备。
    """
    if not ips:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def guarded(ip: str) -> bool:
        async with sem:
            return await _probe_alive_async(ip, ports, timeout_s)

    async def run_all() -> list[bool]:
        return list(await asyncio.gather(*(guarded(ip) for ip in ips)))

    try:
        return asyncio.run(run_all())
    except RuntimeError:
        # 已在事件循环中（不该发生——调用方在 worker 线程）：退回线程池版本
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            return list(pool.map(lambda ip: probe_alive(ip, ports, timeout_s), ips))


def scan_cidr(
    cidr: str,
    idn_contains: str,
    timeout_ms: int = SCAN_TIMEOUT_MS,
    prefilter: bool = True,
) -> list[FindResult]:
    """并发扫描网段内全部地址，返回所有在线设备的 FindResult（含非目标设备）。

    prefilter=True（默认）先经 TCP 端口预筛淘汰无仪器服务的地址，再对候选做 VISA
    *IDN? 识别（纯 VISA 全网段实测约 8.5 分钟，预筛后约数秒）。
    每个在线设备实时打印留痕（[HIT] 为 IDN 匹配目标）；无响应地址静默跳过。
    """
    net = ipaddress.ip_network(cidr, strict=False)
    addrs = [str(h) for h in net.hosts()]
    print(f"[scan] 网段 {cidr} 共 {len(addrs)} 个地址，workers={SCAN_WORKERS}", file=sys.stderr)

    if prefilter:
        t0 = time.monotonic()
        if len(addrs) > 1024:
            # 大网段（掩码比 /24 宽，如本机仪器网 192.168.0.0/16）走**分块异步**探测：
            # 线程版 128 并发 × 0.6s 超时对 6.5 万地址要约 5 分钟，会撞 MCP 的
            # discover 预算（300s）；异步版实测单端口 /16 约 45s（2026-09-16）。
            open_map = probe_open_ports(addrs, SCAN_PROBE_PORTS)
            alive = [a for a in addrs if open_map.get(a)]
        else:
            open_map = {}
            with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
                ok_flags = list(pool.map(probe_alive, addrs))
            alive = [a for a, ok in zip(addrs, ok_flags) if ok]
        print(
            f"[scan] 端口预筛({','.join(map(str, SCAN_PROBE_PORTS))}) "
            f"候选 {len(alive)}/{len(addrs)}，耗时 {time.monotonic() - t0:.1f}s",
            file=sys.stderr,
        )
        for a in alive:
            print(f"[scan] 候选: {a}", file=sys.stderr)
        addrs = alive
    else:
        open_map = {}

    needle = idn_contains.lower()
    hits: list[FindResult] = []
    if not addrs:
        return hits

    # 识别阶段走 `identify_lan_all`：**共享一个 ResourceManager**。
    # 为什么（2026-09-16 设计复核）：这里原先 `pool.submit(identify_lan, a, timeout_ms)`
    # 每台候选各自 `ResourceManager()`，正是 `identify_lan` 文档里写明"多线程各自建 RM
    # 会随机抛 VI_ERROR_INV_OBJECT 并冲垮整轮扫描"的写法——`instr_discover` 早先已改成
    # 共享单例，库路径（`find_device`/`resolve` 的回退）却漏了，两条路径行为不一致。
    # 顺带把预筛到的开放端口传下去，让识别只试**对应协议**（省掉逐协议空等）。
    found_map = identify_lan_all(addrs, timeout_ms=timeout_ms, open_ports=open_map or None)
    for i, addr in enumerate(addrs, 1):
        found = found_map.get(addr)
        if not found:
            continue
        res, idn = found
        mark = "HIT" if needle in idn.lower() else "dev"
        print(f"[scan {i}/{len(addrs)}] [{mark}] {res} -> {idn}", file=sys.stderr)
        hits.append(FindResult(resource=res, idn=idn, source="scanned"))
    return hits


def find_device(
    idn_contains: str,
    resource: Optional[str] = None,
    hosts: Optional[list[str]] = None,
    allow_scan: bool = False,
    cidr: Optional[str] = None,
    timeout_ms: int = 3000,
    prefilter: bool = True,
) -> FindResult:
    """按查找链发现 *IDN? 含 idn_contains 的设备，未找到抛 RuntimeError。

    层① resource 完整资源串（USB/TCPIP 均可）→ 层② hosts（IP/host 列表，
    按 LAN_PROTOCOLS 自动选协议：VXI-11 / HiSLIP / raw5025-SOCKET）→
    层③ list_resources 全扫 → 层④ CIDR 网段扫描。
    层③④ 仅在未给显式参数、或显式全失败且 allow_scan=True 时执行；
    prefilter 仅对层④生效：True 先 TCP 端口预筛再 VISA 识别（推荐）。
    要固定协议时把完整资源串传给 resource（如 TCPIP0::ip::hislip0::INSTR）。
    """
    needle = idn_contains.lower()

    def matches(idn: str) -> bool:
        return needle in idn.lower()

    attempts: list[tuple[str, str, str]] = []
    mismatched: list[tuple[str, str]] = []
    explicit_failed = False

    explicit_targets: list[str] = ([resource] if resource else []) + list(hosts or [])
    for target in explicit_targets:
        hit: Optional[tuple[str, str]] = None
        if "::" in target:
            idn = identify(target, timeout_ms)
            hit = (target, idn) if idn else None
        else:
            hit = identify_lan(target, timeout_ms)
        if hit is None:
            explicit_failed = True
            attempts.append(("explicit", target, "无响应/打开失败"))
            print(f"[explicit] [--] {target}", file=sys.stderr)
            continue
        res, idn = hit
        attempts.append(("explicit", res, idn))
        print(f"[explicit] [{'HIT' if matches(idn) else 'dev'}] {res} -> {idn}", file=sys.stderr)
        if matches(idn):
            return FindResult(resource=res, idn=idn, source="explicit")
        mismatched.append((res, idn))
    if mismatched:
        found = "; ".join(f"{res} -> {idn}" for res, idn in mismatched)
        raise ValueError(
            f"显式指定的设备在线但不是目标（期望 *IDN? 含 {idn_contains!r}）：{found}；拒绝自动换设备"
        )

    def try_listed() -> Optional[FindResult]:
        listed = list_resources()
        # 串口先经子进程并行探测（本进程线程探测会留下卡死线程 → 后续 VISA 调用
        # 令进程崩溃，详见 probe_serial_isolated 说明）；非串口在本进程直接识别。
        serial_res = [r for r in listed if r.upper().startswith("ASRL")]
        serial_idn: dict[str, Optional[str]] = {}
        if serial_res:
            for entry in probe_serials_isolated(serial_res, timeout_ms):
                serial_idn[entry["resource"]] = entry.get("idn")
                note = entry.get("note") or entry.get("idn") or "无响应"
                print(f"[listed] [{'HIT' if entry.get('idn') and matches(entry['idn']) else '--'}] "
                      f"{entry['resource']} -> {note}", file=sys.stderr)

        for res in listed:
            if res.upper().startswith("ASRL"):
                idn = serial_idn.get(res)
                attempts.append(("listed", res, idn or "无响应/打开失败/占用"))
            else:
                idn = identify(res, timeout_ms)
                attempts.append(("listed", res, idn or "无响应/打开失败"))
                print(f"[listed] [{'HIT' if idn and matches(idn) else '--'}] {res}"
                      + (f" -> {idn}" if idn else ""), file=sys.stderr)
            if idn and matches(idn):
                return FindResult(resource=res, idn=idn, source="listed")
        return None

    if not explicit_targets or (explicit_failed and allow_scan):
        hit = try_listed()
        if hit:
            return hit

    if not allow_scan:
        attempts.append(("scanned", "-", "allow_scan=False，跳过网段扫描"))
    else:
        # 显式 cidr 优先；否则扫本机**所有**接口所在的私网网段（多网卡/挂 VPN 时
        # 只看默认路由那张网卡会选错网段，详见 local_cidrs 说明）。
        # 由窄到宽：近邻 /24 先扫（~5s），未命中才放宽到真实网段（可能 ~166s）
        segments = local_scan_segments([cidr] if cidr else None)
        if not segments:
            attempts.append(("scanned", "-", "无法探测本机网段且未显式给 cidr"))
        else:
            if not cidr:
                print(f"[scan] 未指定网段，自动探测为本机接口网段 {segments}"
                      "（多网卡环境建议显式传 cidr）", file=sys.stderr)
            for segment in segments:
                for cand in scan_cidr(segment, idn_contains, SCAN_TIMEOUT_MS, prefilter=prefilter):
                    if matches(cand.idn):
                        return cand

    chain = "\n".join(f"  [{layer}] {target}: {result}" for layer, target, result in attempts)
    raise RuntimeError(f"未找到 *IDN? 含 {idn_contains!r} 的设备，尝试链路：\n{chain}")


def _main() -> None:
    for res, idn in scan().items():
        print(f"{res:45s} -> {idn or '(no response)'}", file=sys.stderr)


if __name__ == "__main__":
    _main()
