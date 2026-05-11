# mcp_guard_v2.ps1 - mcp-server-v2 watchdog (v3 - heartbeat file check)
# Invoke-WebRequest 縺ｯ SYSTEM 繧ｳ繝ｳ繝・く繧ｹ繝医〒荳榊ｮ牙ｮ壹↑縺溘ａ
# V2 縺梧嶌縺上ワ繝ｼ繝医ン繝ｼ繝医ヵ繧｡繧､繝ｫ縺ｧ逕滓ｭｻ蛻､螳壹☆繧・

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
    # 1. PID繝輔ぃ繧､繝ｫ縺悟ｭ伜惠縺励√・繝ｭ繧ｻ繧ｹ縺檎函縺阪※縺・ｋ縺狗｢ｺ隱・
    if (-not (Test-Path $PIDFILE)) { return $false }
    $pidVal = Get-Content $PIDFILE -ErrorAction SilentlyContinue
    if (-not ($pidVal -match '^\d+$')) { return $false }
    $proc = Get-Process -Id ([int]$pidVal) -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }

    # 2. 繝上・繝医ン繝ｼ繝医ヵ繧｡繧､繝ｫ縺・60 遘剃ｻ･蜀・↓譖ｴ譁ｰ縺輔ｌ縺ｦ縺・ｋ縺・
    if (Test-Path $HEARTBEAT) {
        $age = (Get-Date) - (Get-Item $HEARTBEAT).LastWriteTime
        if ($age.TotalSeconds -lt 60) { return $true }
        # 繝上・繝医ン繝ｼ繝医′蜿､縺・竊・繧ｾ繝ｳ繝薙・繝ｭ繧ｻ繧ｹ縺ｮ蜿ｯ閭ｽ諤ｧ
        Write-Log "WARN: heartbeat stale ($([int]$age.TotalSeconds)s old)"
        return $false
    }

    # 繝上・繝医ン繝ｼ繝医ヵ繧｡繧､繝ｫ縺ｪ縺・竊・襍ｷ蜍慕峩蠕後°繧ゅ＠繧後↑縺・・縺ｧ繝励Ο繧ｻ繧ｹ縺縺代〒蛻､螳・
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
    # netstat 縺ｧ繧ょｿｵ縺ｮ繧・
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
    # [2026-04-26 C1] dispatcher 一本化: legacy tools/task.py の代わりに
    # tools/task_v2.py + dispatcher.py + backend_cli.py 経由で run_task を処理。
    # 観察期間 3 日 (2026-04-29 まで)、問題なければ legacy 退避予定。
    $env:V2_USE_DISPATCHER = '1'
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
    # Wait up to 180s (180 iterations x 1s) for V2 to write its PID file.
    # (Old comment claimed 20s which was wrong by 9x; left here in English so
    #  future readers see the corrected bound regardless of cp932 mojibake.)
    # 譛螟ｧ 20 遘貞ｾ・ｩ滂ｼ・ID繝輔ぃ繧､繝ｫ縺梧嶌縺九ｌ繧九∪縺ｧ・・
    for ($i = 0; $i -lt 180; $i++) {
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
