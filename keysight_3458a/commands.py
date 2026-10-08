"""HP/Keysight 3458A 八位半万用表命令常量——**非 SCPI**，本文件即命令白名单。

3458A 与标准 SCPI 仪表（如本仓 keysight_3446x 的 34465A）不是一套语法：

    - 没有 `*IDN?`（用 `ID?`）、没有 `SYST:ERR?`（用 `ERRSTR?`）、没有 `*RST`（用 `RESET`）；
    - 读数是 `TARM SGL,1` 触发后**直接回值**（命令本身不带问号）；
    - 档位/积分时间用 `DCV <range>` / `NPLC <n>`，不是 `:SENS:VOLT:DC:...`；
    - **串尾必须是 LF**：CRLF 会让它不应答（实测，见 [VISA] L529-531）。

因此本库不套用 `common/visa_client.py` 的 SCPI 假设，也不走 `instr_query` 通用工具。

**白名单纪律**（AGENTS.md 铁律 1「命令禁止猜测」）：本文件只允许出现下方已登记的命令。
新增任何命令前，必须先在 `docs/COMMANDS_3458A.md` 里登记并写明出处（手册章节 / 实测留痕），
不允许"顺手加"（例如 OHM/DCI/AZERO/FUNC?/RANGE? 都不在本库范围内）。

出处代号（逐条标注）：
    [SICL]    EmoeCalibrator/Software/cal_tool/dmm_sicl.py（实战版 SICL 驱动，行号见下）
    [VISA]    EmoeCalibrator/Software/cal_tool/cal_devices.py::DMM3458A_VISA
    [SAMPLE]  EmoeCalibrator/3458/python3458A-100k/python3458A_100k.py（Keysight 官方样例）
    [SAMPLE2] EmoeCalibrator/3458/python3458A-100k/python3458A_10V_100NPLC.py
    [TOOLS]   EmoeCalibrator/Software/cal_tool/tools/tc_attrib_temp.py 等
              （在 3458A 会话上查内部温度 TEMP?）
    [MEASURED] 本项目 2026-09-23 在本机 GPIB0::9（82357B/Keysight VISA）**真机实测**
              通过的查询——最高等级证据，逐条记响应样例
    [AC]      EmoeCalibrator/Software/cal_tool/ac_1khz_probe.py / ac_stability.py /
              ac_verify.py（3458A 交流测量配方：SETACV / ACBAND / ACV）
    [MANUAL]  Agilent 3458A User's Guide（本机
              `E:\手册与技术支持\设备资料与文档\3458A\Ag_3458A_UserGuide_en.pdf`）——
              2026-09-23 逐条核对；页码索引见 `docs/3458a_manual_verification_20260923.md`
    [TASK]    仅来自本项目任务书、**参考实现与实测均无** → 已在
              docs/COMMANDS_3458A.md「待手册核对项」登记，代码里一律标注"未验证"
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 身份 / 状态
# ---------------------------------------------------------------------------
ID = "ID?"                       # [SICL] L392-394；[VISA] L639-640 —— 不是 *IDN?
ERRSTR = "ERRSTR?"               # [SAMPLE] L39 —— 不是 SYST:ERR?；每次查询弹出队首错误
TEMP = "TEMP?"                   # [TOOLS] tc_attrib_temp.py:96、tc_temp_sweep.py:135
                                 #   —— 3458A 内部温度。[MANUAL] p.37/p.50：
                                 #   单位=**摄氏度**（实测 37.0/36.9 合理）

# ---------------------------------------------------------------------------
# 生命周期 / 总线
# ---------------------------------------------------------------------------
RESET = "RESET"                  # [SICL] L320；[VISA] L569；[SAMPLE] L35 —— 不是 *RST
END_ALWAYS = "END ALWAYS"        # [SICL] L325；[VISA] L571 —— 每次读数都置 EOI
INBUF_ON = "INBUF ON"            # [SICL] L326；[VISA] L575 —— 手册 TARM 章节要求：
                                 #   用 TARM SGL 触发单次读数时必须打开输入缓冲
                                 #   （或抑制 CR LF），否则 GPIB 总线会被占住

# ---------------------------------------------------------------------------
# 状态回读（**2026-09-23 本机 GPIB0::9 实测：这些查询都有响应**，见 docs/COMMANDS_3458A.md）
# ---------------------------------------------------------------------------
TARM_Q = "TARM?"                 # [MEASURED] 实测 '4'（HOLD）/ '1'（AUTO）
TRIG_Q = "TRIG?"                 # [MEASURED] 实测 '1'（AUTO）/ '4'（HOLD）
NRDGS_Q = "NRDGS?"               # [MEASURED] 实测 '1, 1'
NPLC_Q = "NPLC?"                 # [MEASURED] 实测 '10.0000000E+00'
APER_Q = "APER?"                 # [MEASURED] 实测 '200.000000E-03'
FUNC_Q = "FUNC?"                 # [MEASURED] 实测 '1, .1'（功能码 + 档位）
RANGE_Q = "RANGE?"               # [MEASURED] 实测 '.1'
AZERO_Q = "AZERO?"               # [MEASURED] 实测 '1'
MEM_Q = "MEM?"                   # [MEASURED] 实测 '0'
OFORMAT_Q = "OFORMAT?"           # [MEASURED] 实测 '1'（ASCII）
MFORMAT_Q = "MFORMAT?"           # [MEASURED] 实测 '4' = **SREAL**（[MANUAL] p.199 码表，
                                 #   SINT 是 2；上电默认就是 SREAL，别读成 SINT）
INBUF_Q = "INBUF?"               # [MEASURED] 实测 '1'（ON；[MANUAL] p.186-187 上电 OFF/默认 ON）
END_Q = "END?"                   # [MEASURED] 实测 '2'（ALWAYS；[MANUAL] p.176 0/1/2）

# ---------------------------------------------------------------------------
# 手册核对过的数值码表（[MANUAL] 页码见 docs/3458a_manual_verification_20260923.md）
# ---------------------------------------------------------------------------
TARM_CODES = {1: "AUTO", 2: "EXT", 3: "SGL", 4: "HOLD"}          # p.251
TRIG_CODES = {1: "AUTO", 2: "EXT", 3: "SGL", 4: "HOLD"}          # p.257
END_CODES = {0: "NEVER", 1: "ON", 2: "ALWAYS"}                   # p.176
INBUF_CODES = {0: "OFF", 1: "ON"}                                # p.186-187
FORMAT_CODES = {1: "ASCII", 2: "SINT", 3: "DINT", 4: "SREAL"}    # p.199 (MFORMAT) / p.210 (OFORMAT)
AZERO_CODES = {0: "OFF", 1: "ON", 2: "ONCE"}                     # p.162-163
TARM_SGL_MAX_ARMS = 2.1e9       # [MANUAL] p.251：TARM SGL 的 number_arms 上限
NRDGS_MAX = 16_777_215          # [MANUAL] p.207：NRDGS 的 n 上限（1..16777215）

# ---------------------------------------------------------------------------
# 自动挡 / 固定档（2026-09-23 按手册补齐：此前只支持固定档）
# ---------------------------------------------------------------------------
ARANGE = "ARANGE"                # [MANUAL] p.160-161：Autorange ON / OFF / ONCE
ARANGE_Q = "ARANGE?"             # [MANUAL] p.161 查询命令
ARANGE_ON = "ARANGE ON"
ARANGE_OFF = "ARANGE OFF"
DCV_AUTO = "DCV AUTO"            # [MANUAL] p.184：max_input=AUTO → autorange 模式
ARANGE_CODES = {0: "OFF", 1: "ON", 2: "ONCE"}   # 码值现场标定（ARANGE ON/OFF 实测）

# ---------------------------------------------------------------------------
# 大信号：DINT 通路（[MANUAL] p.173：direct-sampling 用 DINT 时满量程 = 档位 ×500%）
# ---------------------------------------------------------------------------
MFORMAT_DINT = "MFORMAT DINT"    # [MANUAL] p.199：DINT = 码 3，4 字节/读数
OFORMAT_DINT = "OFORMAT DINT"    # [MANUAL] p.210：同上
DINT_BYTES = 4                   # 每读数 4 字节（32 位 2 的补码）
PRESET_NORM = "PRESET NORM"      # [MANUAL] p.217：退出数字档回"正常远程测量"预设
                                 #   （原文"similar to RESET but optimizes for remote
                                 #   operation"；不是 RESET 命令）

# ---------------------------------------------------------------------------
# 触发模型
# ---------------------------------------------------------------------------
TARM_HOLD = "TARM HOLD"          # [SICL] L318 —— 停止后续触发（free-run 的第一道闸）
TARM_AUTO = "TARM AUTO"          # [MANUAL] p.251（码 1）—— 连续触发，前面板持续刷新读数
                                 #   release_to_front_panel() 的收尾用；2026-10-08 引入
TARM_SGL_1 = "TARM SGL,1"        # [SICL] L370；[VISA] L615 —— 触发**一次**读数并回值
TARM_SYN = "TARM SYN"            # [SAMPLE] L64 —— 同步触发：进入等待，由后续事件/读数取走
TRIG_HOLD = "TRIG HOLD"          # [SICL] L319；[SAMPLE2] L48 —— 触发源保持（不自动重触发）
TRIG_AUTO = "TRIG AUTO"          # [SAMPLE] L56 —— 自动触发（数字突发配方里用）

# ---------------------------------------------------------------------------
# 直流电压
# ---------------------------------------------------------------------------
DCV = "DCV"                      # [SICL] L333；[VISA] L581 —— 参数 = 档位（V），不是查询
NPLC = "NPLC"                    # [SICL] L334；[VISA] L582 —— 参数 = 积分时间（PLC 倍数）

# ---------------------------------------------------------------------------
# 交流电压
# ---------------------------------------------------------------------------
ACV = "ACV"                      # [AC] ac_stability.py:59 一带 —— 参数 = 档位（V）
SETACV_ANA = "SETACV ANA"        # [AC] ac_1khz_probe.py:84、ac_verify.py:130
                                 #   —— 模拟（模拟真有效值）转换，>10Hz 用
SETACV_SYNC = "SETACV SYNC"      # [AC] ac_stability.py:55、ac_verify.py:125
                                 #   —— 同步（采样）转换，<10Hz 用
ACBAND = "ACBAND"                # [AC] ac_1khz_probe.py:85、ac_verify.py:133
                                 #   —— 参数 = "<下限Hz>,<上限Hz>"（影响起伏与噪声）

# ---------------------------------------------------------------------------
# 高速采样 / 二进制突发（Keysight 官方 100k rdg/s 样例配方）
# ---------------------------------------------------------------------------
PRESET_DIG = "PRESET DIG"        # [SAMPLE] L43 —— 数字表预设（整组采样参数复位到数字档）
MFORMAT_SINT = "MFORMAT SINT"    # [SAMPLE] L45 —— 内存格式 = 2 字节有符号整数
OFORMAT_SINT = "OFORMAT SINT"    # [SAMPLE] L46 —— 输出格式 = 2 字节有符号整数
MEM_OFF = "MEM OFF"              # [SAMPLE] L49 —— 关读数内存（直接走总线，不存表内）
TIMER = "TIMER"                  # [SAMPLE] L48 —— 参数 = 采样间隔（s）
TIMER_Q = "TIMER?"               # [MANUAL] p.256：查询当前采样间隔（突发超时预算要用）
APER = "APER"                    # [SAMPLE] L47；[SAMPLE2] L52 —— 参数 = 孔径时间（s）
NRDGS = "NRDGS"                  # [SAMPLE] L55 —— 参数 = 一次触发的读数个数
ISCALE_Q = "ISCALE?"             # [SAMPLE] L59 —— 查询 SINT 读数的换算因子（V/LSB）
                                 #   （SINT 是整数量化值，真值 = 整数 × ISCALE）

# ---------------------------------------------------------------------------
# 档位与配方常数
# ---------------------------------------------------------------------------
# [SICL] L248；[VISA] L466 —— 直流电压档位全集
DCV_RANGES = (0.1, 1.0, 10.0, 100.0, 1000.0)

# [VISA] L594 —— 10V 档有 20% 超量程（可用到 ±12V）。
# ⚠ range_for() 仍按 1.1 倍余量选档（与参考实现一致、偏保守）：11V 会被选到 100V 档。
#   确知信号 ≤12V 且要用 10V 档时，显式 configure_dcv(10.0) / set_range(10.0)。
DCV_10V_OVERLOAD_V = 12.0

# [SAMPLE] L47 的孔径；[SAMPLE] L48 的采样间隔（100k rdg/s 配方，>=10µs 才到得了 100k）
DEFAULT_APERTURE_S = 1.4e-6
DEFAULT_SAMPLE_INTERVAL_S = 10e-6

# 一次突发允许的最大读数个数 = **设备上限**（[MANUAL] p.207：NRDGS 的 n ∈ 1..16777215）。
# 提醒：SINT 时一次要读 2n+2 字节，n=16.7M ≈ 33 MB 且按 100k rdg/s 约需 3 分钟——
# 库层不再自设 1e6 的小上限，但调用方要为内存/时间负责（MCP 工具只回摘要、全量走 CSV）。
BURST_MAX_READINGS = NRDGS_MAX
# 突发回读超时预算：`timeout = clamp(10s + 3 × n × 采样间隔, 10s, 180s)`。
# 现场教训（2026-09-23）：MCP 进程里一次 burst 卡住 71s+ 并**握着设备锁**，
# 把后续所有调用堵死——根因是回读用了通用超时（120s）且异常路径没收尾。
BURST_TIMEOUT_MIN_S = 10.0
BURST_TIMEOUT_MAX_S = 180.0


def fmt_num(value: float) -> str:
    """数值 → 3458A 命令参数文本（大写 E 指数量级，与 Keysight 样例风格一致）。

    样例写 `TIMER 10E-6` / `APER 1.4E-6`，而 Python 的 `%g` 产出小写 `1.4e-06`。
    大小写与指数前导零在 3458A 上是否等价**未实测**，这里按样例形态产出
    （`1.4E-6` / `1E-5`），避免把"格式差异"混进将来的真机排错。
    """
    text = f"{float(value):g}"
    if "e" in text or "E" in text:
        mantissa, _, exponent = text.replace("E", "e").partition("e")
        text = f"{mantissa}E{int(exponent)}"
    return text.upper()
