"""FFT 判定真实采样间隔（最鲁棒，不受过零噪声影响）。

方法：读 50000 点 → FFT 找主频 bin → 若假设 interval=1ns 时主频为 f1，
      则真实 interval = f1/f_measured × 1ns（f_measured 为设备硬件测量值）。
"""
import struct
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from common.resolver import resolve
from sds_control import SDS

# 地址由 common.resolver 解析（不写死 IP，换网段/换口自适应）
s = SDS(resolve("sds"), timeout_ms=20000)
s.connect()
try:
    # 当前模式与采样率
    mman = s.query("ACQ:MMAN?").strip()
    srat = s.query("ACQ:SRAT?").strip()
    poin = s.query("ACQ:POIN?").strip()
    tdiv = s.query("TDIV?").strip()
    f_meas = s.measure_simple("FREQ", "C2", timeout_s=8.0)
    print(f"MMAN={mman} SRAT={srat} POIN={poin} TDIV={tdiv}", flush=True)
    print(f"设备硬件测量 FREQ = {f_meas:.2f} Hz", flush=True)

    # 读数据
    s.write(":WAVeform:SOURce C2")
    s.write("WAV:WIDT WORD")
    s.write("WAV:POIN 50000")
    s.write("WAV:STAR 0")
    time.sleep(0.5)
    data = s.query_raw("WAV:DATA?")
    i = data.find(b"#")
    nd = int(data[i + 1: i + 2])
    ln = int(data[i + 2: i + 2 + nd])
    blob = data[i + 2 + nd: i + 2 + nd + ln]
    codes = np.frombuffer(blob, dtype="<i2").astype(float)
    n = len(codes)
    print(f"读取 {n} 点", flush=True)

    # FFT 主频（假设 interval=1ns）
    x = codes - codes.mean()
    win = np.hanning(n)
    spec = np.abs(np.fft.rfft(x * win))
    freqs_if_1ns = np.fft.rfftfreq(n, d=1e-9)   # 假设 1ns 时的频率轴
    peak_bin = int(np.argmax(spec[1:]) + 1)
    f_if_1ns = freqs_if_1ns[peak_bin]
    print(f"FFT 主峰: bin={peak_bin} → 若 interval=1ns 则频率={f_if_1ns/1e3:.2f} kHz", flush=True)

    # 真实 interval
    real_interval = f_if_1ns / f_meas * 1e-9
    print(f"\n推算真实 interval = {real_interval*1e9:.4f} ns "
          f"→ 采样率 = {1/real_interval/1e6:.2f} MSa/s", flush=True)
    print(f"（ACQ:SRAT? 声称 {srat}；比值 = {real_interval/1e-9:.4f}）", flush=True)

    # 用真实 interval 反算频率，应与设备测量一致
    print(f"验证: 用该 interval 算主频 = {f_if_1ns * 1e-9 / real_interval / 1e3:.2f} kHz "
          f"vs 设备测量 {f_meas/1e3:.2f} kHz", flush=True)

    # 次峰（确认非谐波误判）
    order = np.argsort(spec[1:])[::-1][:5] + 1
    print("\n前 5 个谱峰（若 interval=1ns 的频率轴）:", flush=True)
    for b in order:
        print(f"  {freqs_if_1ns[b]/1e3:10.2f} kHz  幅值 {spec[b]:.0f}", flush=True)
finally:
    s.close()
