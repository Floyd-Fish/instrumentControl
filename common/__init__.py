"""instrumentControl 通用层：VISA 客户端 + 统一发现 + 地址解析（多设备共用）。

用法：
    from common import find_device, FindResult, resolve

    hit = find_device("DH1766", hosts=["192.168.1.100"])   # 显式 LAN（示例地址）
    hit = find_device("DH1766", allow_scan=True, cidr="192.168.1.0/24")  # 最后手段

    res = resolve("sds")   # 地址解析：显式 > env > 配置 > 上次成功缓存 > 自动发现
                           # （不写死 IP：地址随 DHCP/换网段/换口变化）
"""
import os as _os

# 运行环境预置：必须在任何会拉起 numpy 的三方库（pyvisa 的 util.py 模块级 import numpy）
# **之前**执行——本模块是所有设备库的第一个 import，放这里能让**脚本直连模式**与
# MCP 模式同样受益（2026-10-08 实测：import pyvisa 私有提交 754.8 MB → 15.5 MB；
# 根因是 32 线程 CPU 上 OpenBLAS 线程池 ~23 MB/线程。setdefault 允许启动方覆盖）。
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from .discovery import (
    LAN_PROTOCOLS,
    FindResult,
    detect_cidr,
    find_device,
    identify,
    identify_all,
    identify_lan,
    identify_lan_all,
    local_cidrs,
    proxy_likely,
    list_resources,
    probe_alive,
    probe_alive_many,
    probe_open_ports,
    scan,
    scan_cidr,
    tcpip_resource,
)
from .resolver import (
    CACHE_FILE,
    CONFIG_DIR,
    CONFIG_FILE,
    DEVICE_KINDS,
    autofill_config,
    canonicalize,
    clear_config,
    config_template,
    explain,
    idn_kind,
    is_visa_resource,
    known_resources,
    remember,
    remember_candidates,
    resolve,
    save_config,
)
from .visa_client import VisaClient

__all__ = [
    "CACHE_FILE",
    "CONFIG_DIR",
    "CONFIG_FILE",
    "DEVICE_KINDS",
    "LAN_PROTOCOLS",
    "FindResult",
    "VisaClient",
    "autofill_config",
    "canonicalize",
    "clear_config",
    "config_template",
    "detect_cidr",
    "explain",
    "find_device",
    "idn_kind",
    "identify",
    "identify_all",
    "identify_lan",
    "identify_lan_all",
    "local_cidrs",
    "proxy_likely",
    "is_visa_resource",
    "known_resources",
    "list_resources",
    "probe_alive",
    "probe_alive_many",
    "probe_open_ports",
    "remember",
    "remember_candidates",
    "resolve",
    "save_config",
    "scan",
    "scan_cidr",
    "tcpip_resource",
]
