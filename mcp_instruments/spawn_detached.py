# -*- coding: utf-8 -*-
"""全脱离服务启动器（run_remote.ps1 专用）。

用法：python spawn_detached.py <python.exe> <server.py> <out.log> <err.log>
行为：以 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
     起服务进程（stdout/stderr 重定向到日志文件），**打印服务 pid 后立即退出**。

为什么存在（2026-10-08 实测三种方案后定型）：
  * Start-Process -RedirectStandard* ：pwsh 持有输出文件句柄，服务不退出则
    宿主（opencode shell / WinRM）的输出捕获等到超时——start 逻辑早完成，
    纯显示层卡死（实测 3 分钟+），期间误判还会清理状态文件留下僵尸服务；
  * cmd /c start /b               ：服务与本脚本共享 console，宿主仍等；
  * 本启动器（os.spawn + DETACHED）：从根上断开 console/句柄/Job 关系，
    宿主立即拿到返回，服务独立存活（计划任务同款拓扑）。
"""
import os
import sys

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: spawn_detached.py <python.exe> <server.py> <out.log> <err.log>",
              file=sys.stderr)
        return 2
    exe, srv, out_log, err_log = sys.argv[1:5]

    import subprocess
    out_f = open(out_log, "ab", buffering=0)
    err_f = open(err_log, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            [exe, srv],
            stdin=subprocess.DEVNULL, stdout=out_f, stderr=err_f,
            creationflags=(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                           | CREATE_BREAKAWAY_FROM_JOB),
            close_fds=True,
        )
        pid = proc.pid
        # 不 wait、不持有管道——句柄随本进程退出关闭（子进程已复制自己的副本）
    finally:
        out_f.close()
        err_f.close()
    print(pid)          # 唯一输出：服务 pid（pwsh 侧捕获）
    return 0


if __name__ == "__main__":
    sys.exit(main())
