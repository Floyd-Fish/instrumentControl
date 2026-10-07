#requires -Version 7
<#
.SYNOPSIS
    instrumentControl MCP 单例服务管理：start / stop / restart / status。

.DESCRIPTION
    把 MCP server 起成**一个常驻 streamable-http 进程**，供所有 OpenCode 项目共用，
    取代原先"每个工作目录各起一个 stdio 子进程"（9~10 份 × ~755 MB 私有提交）。

    运维模型对齐 kimi-webbridge 的 daemon 约定：
      * **start 幂等**——已在跑就只报告地址，不重复起；
      * **绝不静默换端口**——端口被别的程序占用时报错退出，让人显式改 -Port；
      * **状态文件是唯一事实来源**——pid/addr 文件记录活进程，陈旧残留由 start 清理；
      * **日志带时间戳**，上一次运行保留为 *.prev.log。

    回滚：不设 MCP_TRANSPORT 时 server.py 仍走 stdio；本脚本不改 OpenCode 配置。

.PARAMETER Action
    start（默认）| stop | restart | status

.PARAMETER Port
    监听端口。默认取环境变量 MCP_HTTP_PORT，再默认 8765。

.PARAMETER PythonExe
    python 解释器路径。默认探测：PATH 上的 python → py -3 → 报错。

.PARAMETER TimeoutSec
    启动后等待健康检查的总秒数（默认 45；冷启动实测约 7 s）。

.EXAMPLE
    pwsh -File run_remote.ps1 start
    pwsh -File run_remote.ps1 status
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action = 'start',
    [int]$Port = 0,
    [string]$PythonExe = '',
    [int]$TimeoutSec = 45
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

# ── 路径锚定到脚本自身（不依赖 CWD）────────────────────────────────────
$ScriptDir = $PSScriptRoot
$ServerPy  = Join-Path $ScriptDir 'server.py'
$RunDir    = Join-Path $ScriptDir 'run'
$LogDir    = Join-Path $RunDir 'logs'
$PidFile   = Join-Path $RunDir 'mcp_server.pid'
$AddrFile  = Join-Path $RunDir 'mcp_server.addr'

if (-not (Test-Path -LiteralPath $ServerPy)) {
    throw "server.py not found next to this script: $ServerPy"
}

function Resolve-Port {
    if ($Port -gt 0) { return $Port }
    if ($env:MCP_HTTP_PORT) {
        $p = 0
        if ([int]::TryParse($env:MCP_HTTP_PORT, [ref]$p) -and $p -gt 0 -and $p -lt 65536) { return $p }
        throw "invalid MCP_HTTP_PORT='$($env:MCP_HTTP_PORT)'"
    }
    return 8765
}

function Resolve-Python {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path -LiteralPath $Explicit)) { throw "python not found: $Explicit" }
        return (Resolve-Path -LiteralPath $Explicit).Path
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return $py.Source }   # py -3 launcher; caller passes exe only
    throw "python interpreter not found; pass -PythonExe <path>"
}

function Get-LivePid {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $raw = (Get-Content -LiteralPath $PidFile -Raw -ErrorAction SilentlyContinue)
    $procId = 0
    if (-not [int]::TryParse(($raw ?? '').Trim(), [ref]$procId)) { return $null }
    $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    return $procId
}

function Get-ServerAddr {
    if (-not (Test-Path -LiteralPath $AddrFile)) { return $null }
    $a = (Get-Content -LiteralPath $AddrFile -Raw -ErrorAction SilentlyContinue)
    if (-not $a) { return $null }
    return $a.Trim()
}

function Test-McpHealth {
    <# MCP initialize 握手：返回响应文本，失败返回 $null。
       必须是 MCP 应答（含 serverInfo），别的程序占着端口时不会通过。 #>
    param([string]$Base, [int]$TimeoutSec = 6)
    $body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"run_remote-healthcheck","version":"0"}}}'
    $tmp = Join-Path $env:TEMP ('mcp-health-' + [guid]::NewGuid().ToString('N') + '.json')
    [IO.File]::WriteAllText($tmp, $body, [Text.UTF8Encoding]::new($false))
    try {
        # --noproxy "*"：本机 Clash 常驻，代理会吞掉/拖死对 127.0.0.1 的请求。
        $raw = & curl.exe -s --noproxy '*' --max-time $TimeoutSec -X POST "$Base/mcp" `
            -H 'Content-Type: application/json' `
            -H 'Accept: application/json, text/event-stream' `
            --data-binary "@$tmp" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $raw) { return $null }
        if ($raw -match '"serverInfo"') { return $raw }
        return $null
    } catch {
        return $null
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Test-PortListening {
    param([int]$P)
    $c = Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction SilentlyContinue
    return [bool]$c
}

function Rotate-Log {
    # 上一次运行的日志改名为 .prev.log（只保留一代，避免无限增长）
    if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    $prev = Get-ChildItem -LiteralPath $LogDir -Filter 'mcp_server_*.log' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($prev) {
        $target = Join-Path $LogDir 'mcp_server.prev.log'
        Move-Item -LiteralPath $prev.FullName -Destination $target -Force
    }
}

function Start-Server {
    param([int]$P)
    $livePid = Get-LivePid
    $base = "http://127.0.0.1:$P"
    if ($livePid) {
        $h = Test-McpHealth -Base $base
        if ($h) {
            Write-Host "already running (pid=$livePid, $base) — no action taken"
            return
        }
        # 进程还在但不健康：报错让人决定，不静默杀掉（可能正在跑长任务）。
        throw "pid $livePid exists but MCP health check failed at $base; " +
              "run 'run_remote.ps1 stop' first if it is stale"
    }

    if (Test-PortListening -P $P) {
        $owner = (Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction SilentlyContinue |
                  Select-Object -First 1 -ExpandProperty OwningProcess)
        throw "port $P is already held by another process (pid=$owner); " +
              "pick another port: -Port <n>"
    }

    $exe = Resolve-Python -Explicit $PythonExe
    Rotate-Log
    $ts = Get-Date -Format 'yyyyMMdd_HHmmss'
    $outLog = Join-Path $LogDir "mcp_server_$ts.log"
    $errLog = Join-Path $LogDir "mcp_server_$ts.err.log"

    Write-Host "starting MCP server on $base (pid file: $PidFile)"
    $env:PYTHONUTF8 = '1'
    $proc = Start-Process -FilePath $exe `
        -ArgumentList @($ServerPy) `
        -WorkingDirectory $ScriptDir `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
        -Environment @{
            MCP_TRANSPORT      = 'streamable-http'
            MCP_HTTP_HOST      = '127.0.0.1'
            MCP_HTTP_PORT      = "$P"
            PYTHONUTF8         = '1'
            PYTHONUNBUFFERED   = '1'
        }

    # 写状态文件（addr 先写，便于其它工具尽早发现"正在起"）
    if (-not (Test-Path -LiteralPath $RunDir)) { New-Item -ItemType Directory -Path $RunDir -Force | Out-Null }
    [IO.File]::WriteAllText($AddrFile, "127.0.0.1:$P", [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($PidFile, "$($proc.Id)", [Text.UTF8Encoding]::new($false))

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 700
        if ($proc.HasExited) {
            $tail = (Get-Content -LiteralPath $errLog -Tail 15 -ErrorAction SilentlyContinue) -join "`n"
            Remove-Item -LiteralPath $PidFile, $AddrFile -Force -ErrorAction SilentlyContinue
            throw "server exited during startup (exit=$($proc.ExitCode)). stderr tail:`n$tail"
        }
        if (Test-McpHealth -Base $base) {
            Write-Host "OK: MCP server up (pid=$($proc.Id), $base/mcp)"
            Write-Host "    logs: $outLog"
            return
        }
    }

    # 超时：不留下半死进程
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $PidFile, $AddrFile -Force -ErrorAction SilentlyContinue
    $tail = (Get-Content -LiteralPath $errLog -Tail 20 -ErrorAction SilentlyContinue) -join "`n"
    throw "health check timed out after ${TimeoutSec}s; process stopped. stderr tail:`n$tail"
}

function Stop-Server {
    $livePid = Get-LivePid
    if (-not $livePid) {
        # 状态文件可能陈旧；端口上若有别的东西不动它
        Remove-Item -LiteralPath $PidFile, $AddrFile -Force -ErrorAction SilentlyContinue
        Write-Host "not running (no live pid) — state files cleared"
        return
    }
    $addr = Get-ServerAddr
    Write-Host "stopping MCP server pid=$livePid"
    Stop-Process -Id $livePid -Force -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $livePid -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 300
    }
    if (Get-Process -Id $livePid -ErrorAction SilentlyContinue) {
        throw "pid $livePid did not stop within 15s"
    }
    Remove-Item -LiteralPath $PidFile, $AddrFile -Force -ErrorAction SilentlyContinue
    if ($addr) {
        $p = [int]($addr.Split(':')[-1])
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline -and (Test-PortListening -P $p)) { Start-Sleep -Milliseconds 300 }
        if (Test-PortListening -P $p) { Write-Warning "port $p still listening after stop" }
    }
    Write-Host "stopped"
    return
}

function Show-Status {
    $livePid = Get-LivePid
    $addr = Get-ServerAddr
    if (-not $livePid) {
        [pscustomobject]@{ running = $false; addr = $addr; pid = $null } | ConvertTo-Json -Compress
        return
    }
    $base = if ($addr) { "http://$addr" } else { $null }
    $healthy = if ($base) { [bool](Test-McpHealth -Base $base) } else { $false }
    [pscustomobject]@{
        running = $healthy
        pid     = $livePid
        addr    = $addr
        url     = if ($base) { "$base/mcp" } else { $null }
        log     = (Get-ChildItem -LiteralPath $LogDir -Filter 'mcp_server_*.log' -ErrorAction SilentlyContinue |
                   Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName)
    } | ConvertTo-Json -Compress
    return
}

switch ($Action) {
    'start'   { Start-Server -P (Resolve-Port) }
    'stop'    { Stop-Server }
    'restart' { Stop-Server; Start-Server -P (Resolve-Port) }
    'status'  { Show-Status }
}
