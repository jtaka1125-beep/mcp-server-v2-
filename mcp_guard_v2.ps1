# mcp_guard_v2.ps1 - mcp-server-v2 watchdog (v3 - heartbeat file check)
# Invoke-WebRequest は SYSTEM コンテキストで不安定なため
# V2 が書くハートビートファイルで生死判定する

$PORT      = 3001
$SCRIPT    = "C:\MirageWork\mcp-server-v2\server.py"
$PYTHON    = "C:\Windows\py.exe"
$LOG       = "C:\MirageWork\mcp-server-v2\logs\mcp_guard_v2.log"
$PIDFILE   = "C:\MirageWork\mcp-server-v2\server.pid"
$HEARTBEAT = "C:\MirageWork\mcp-server-v2\server.heartbeat"
$WORKDIR   = "C:\MirageWork\mcp-server-v2"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $msg" | Tee-Object -FilePath $LOG -Append
}

function Test-ServerAlive {
    # 1. PIDファイルが存在し、プロセスが生きているか確認
    if (-not (Test-Path $PIDFILE)) { return $false }
    $pidVal = Get-Content $PIDFILE -ErrorAction SilentlyContinue
    if (-not ($pidVal -match '^\d+$')) { return $false }
    $proc = Get-Process -Id ([int]$pidVal) -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }

    # 2. ハートビートファイルが 60 秒以内に更新されているか
    if (Test-Path $HEARTBEAT) {
        $age = (Get-Date) - (Get-Item $HEARTBEAT).LastWriteTime
        if ($age.TotalSeconds -lt 60) { return $true }
        # ハートビートが古い → ゾンビプロセスの可能性
        Write-Log "WARN: heartbeat stale ($([int]$age.TotalSeconds)s old)"
        return $false
    }

    # ハートビートファイルなし → 起動直後かもしれないのでプロセスだけで判定
    return $true
}

function Kill-V2 {
    if (Test-Path $PIDFILE) {
        $old = Get-Content $PIDFILE -ErrorAction SilentlyContinue
        if ($old -match '^\d+$') {
            Stop-Process -Id ([int]$old) -Force -ErrorAction SilentlyContinue
            Write-Log "Killed old V2 PID $old"
        }
        Remove-Item $PIDFILE -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $HEARTBEAT -Force -ErrorAction SilentlyContinue
    # netstat でも念のり
    $lines = netstat -ano 2>$null | Select-String ":$PORT\s" | Where-Object { $_ -match 'LISTENING' }
    foreach ($line in $lines) {
        $pid2 = ($line.ToString().Trim() -split '\s+')[-1]
        if ($pid2 -match '^\d+$' -and [int]$pid2 -gt 4) {
            Stop-Process -Id ([int]$pid2) -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep 2
}

function Start-V2Server {
    Kill-V2
    $env:PATH = "C:\Users\jun\.local\bin;$env:PATH"
    try {
        $cb = (reg query HKCU\Environment /v CEREBRAS_API_KEY 2>$null)
        if ($cb) { $env:CEREBRAS_API_KEY = ($cb -split '\s+')[-1] }
        $gq = (reg query HKCU\Environment /v GROQ_API_KEY 2>$null)
        if ($gq) { $env:GROQ_API_KEY = ($gq -split '\s+')[-1] }
        $gm = (reg query HKCU\Environment /v GEMINI_API_KEY 2>$null)
        if ($gm) { $env:GEMINI_API_KEY = ($gm -split '\s+')[-1] }
    } catch {}
    $p = Start-Process $PYTHON -ArgumentList $SCRIPT -WorkingDirectory $WORKDIR -WindowStyle Hidden -PassThru
    Write-Log "Started V2 PID $($p.Id)"
    # 最大 20 秒待機（PIDファイルが書かれるまで）
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep 1
        if (Test-Path $PIDFILE) {
            $pf = Get-Content $PIDFILE -ErrorAction SilentlyContinue
            if ($pf -match '^\d+$') { return $true }
        }
    }
    return $false
}

Write-Log "mcp_guard_v2 started (heartbeat-check v3)"

while ($true) {
    if (-not (Test-ServerAlive)) {
        Write-Log "WARN: V2 not alive, restarting..."
        $ok = Start-V2Server
        if ($ok) {
            Write-Log "OK: V2 recovered"
        } else {
            Write-Log "ERROR: recovery failed"
        }
    }
    Start-Sleep 30
}
