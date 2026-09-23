# Offline regression batch - 2026-09-23
#
# Runs every TEST_SCRIPTS suite that documents itself as offline (no instrument
# I/O) and records the full output for traceability. Suites that connect to a
# device on purpose (*_live.py, test_*.py, verify_all_devices.py, ...) are NOT
# run here; they belong to a separate live pass and need an explicit go-ahead.
#
# Usage:  pwsh -File TEST_SCRIPTS/common/regression_offline_20260923.ps1
#
# Output: TEST_DATA/common/regression_offline_<stamp>.txt  (full logs)
#         console summary table

$ErrorActionPreference = 'Continue'
$env:PYTHONIOENCODING = 'utf-8'

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $repo

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$outDir = Join-Path $repo 'TEST_DATA\common'
New-Item -ItemType Directory -Force $outDir | Out-Null
$log = Join-Path $outDir "regression_offline_$stamp.txt"

# name -> command (python, relative to repo root)
$suites = [ordered]@{
  'audit_all_commands'        = 'TEST_SCRIPTS/common/audit_all_commands.py'
  'audit_guardrail_coverage'  = 'TEST_SCRIPTS/common/audit_guardrail_coverage.py'
  'verify_audit_extractor'    = 'TEST_SCRIPTS/common/verify_audit_extractor.py'
  'verify_resolver'           = 'TEST_SCRIPTS/common/verify_resolver.py'
  'verify_session_lock'       = 'TEST_SCRIPTS/common/verify_session_lock.py'
  'verify_discovery_local'    = 'TEST_SCRIPTS/common/verify_discovery_local.py'
  'verify_executor_exit'      = 'TEST_SCRIPTS/common/verify_executor_exit.py'
  'verify_remote_lock_block'  = 'TEST_SCRIPTS/common/verify_remote_lock_block.py'
  'verify_rigol_scope_shared' = 'TEST_SCRIPTS/common/verify_rigol_scope_shared.py'
  'verify_rigol_semantics'    = 'TEST_SCRIPTS/common/verify_rigol_scope_semantics.py'
  'verify_mcp_tools_meta'     = 'TEST_SCRIPTS/common/verify_mcp_tools_meta.py'
  'verify_docstring_coverage' = 'TEST_SCRIPTS/common/verify_docstring_trim_coverage_20260923.py'
  'install_skills_safety'     = 'TEST_SCRIPTS/common/verify_install_skills_uninstall.py'
  'k3458a_offline'            = 'TEST_SCRIPTS/ks3458a/verify_3458a_offline.py'
  'k3458a_mcp_tools'          = 'TEST_SCRIPTS/ks3458a/verify_mcp_ks3458a_tools.py'
  'k3458a_worker_isolation'   = 'TEST_SCRIPTS/ks3458a/verify_worker_isolation.py'
  'dg832_edge_cases'          = 'TEST_SCRIPTS/dg832/verify_dg832_edge_cases.py'
  'install_skills_check'      = 'scripts/install_skills.py --check'
}

"=== offline regression $stamp ===" | Out-File -Encoding utf8 $log
"repo: $repo" | Out-File -Encoding utf8 -Append $log
"python: $((python --version 2>&1) -join ' ')" | Out-File -Encoding utf8 -Append $log

$rows = @()
foreach ($name in $suites.Keys) {
  $cmdline = $suites[$name]
  $parts = $cmdline -split ' '
  $t0 = Get-Date
  $text = ''
  if ($parts.Count -gt 1) {
    $text = (& python $parts[0] $parts[1..($parts.Count - 1)] 2>&1 | Out-String)
  } else {
    $text = (& python $parts[0] 2>&1 | Out-String)
  }
  $code = $LASTEXITCODE
  $secs = [int]((Get-Date) - $t0).TotalSeconds

  $pass = ([regex]::Matches($text, '\[PASS\]')).Count
  $fail = ([regex]::Matches($text, '\[FAIL\]')).Count
  $tail = ($text.Trim() -split "`n" | Select-Object -Last 1)
  if ($tail.Length -gt 70) { $tail = $tail.Substring(0, 70) }

  $rows += [pscustomobject]@{
    Suite = $name; Exit = $code; Pass = $pass; Fail = $fail; Secs = $secs; Last = $tail.Trim()
  }

  "" | Out-File -Encoding utf8 -Append $log
  ("=" * 78) | Out-File -Encoding utf8 -Append $log
  "### $name   exit=$code  pass=$pass fail=$fail  ${secs}s" | Out-File -Encoding utf8 -Append $log
  ("=" * 78) | Out-File -Encoding utf8 -Append $log
  $text | Out-File -Encoding utf8 -Append $log
}

"" | Out-File -Encoding utf8 -Append $log
"=== SUMMARY ===" | Out-File -Encoding utf8 -Append $log
$rows | Format-Table -AutoSize | Out-String | Out-File -Encoding utf8 -Append $log

Write-Host ''
Write-Host '=== SUMMARY (exit != 0 or FAIL > 0 shown first) ==='
$rows | Sort-Object @{e={ if ($_.Exit -ne 0 -or $_.Fail -gt 0) { 0 } else { 1 } } }, Suite |
  Format-Table Suite, Exit, Pass, Fail, Secs, Last -AutoSize | Out-String | Write-Host

Write-Host "full log: $log"
