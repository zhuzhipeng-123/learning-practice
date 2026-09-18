param(
    [ValidateSet('Start', 'Stop', 'Status')][string]$Action = 'Start',
    [ValidateRange(1024, 65535)][int]$Port = 8000,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$serviceUrl = "http://127.0.0.1:$Port/"
$logRoot = Join-Path $projectRoot '.runtime'

function Get-LocalService {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if (-not $listeners.Count) { return $null }
    $server = Get-CimInstance Win32_Process -Filter "ProcessId=$($listeners[0].OwningProcess)"
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($server.ParentProcessId)"
    $expectedPrefix = '"' + $pythonPath + '"'
    $owner = @($server, $parent) | Where-Object {
        $_ -and $_.CommandLine -and
        $_.CommandLine.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        $_.CommandLine -match '-m\s+uvicorn\s+app\.main:app'
    } | Select-Object -First 1
    if (-not $owner -or $server.CommandLine -notmatch '-m\s+uvicorn\s+app\.main:app') {
        throw "端口 $Port 被其他程序占用，未停止或更改该程序。请先关闭占用程序，再启动。"
    }
    return $server
}

function Test-Healthy {
    try {
        $health = Invoke-RestMethod ($serviceUrl + 'health') -TimeoutSec 2
        return $health.status -eq 'ok' -and $health.phase -eq 'P0-P6 foundation'
    } catch { return $false }
}

$digest = [Security.Cryptography.SHA256]::Create()
$scope = ([BitConverter]::ToString($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot)))).Replace('-', '').Substring(0, 16)
$digest.Dispose()
$mutex = [Threading.Mutex]::new($false, "Local\LearningPractice-$scope-$Port")
$locked = $false
try {
    $locked = $mutex.WaitOne(0)
    if (-not $locked) { Write-Host '启动或停止操作正在进行，请稍后再试。'; exit 0 }
    $existing = Get-LocalService
    if ($Action -eq 'Stop') {
        if ($existing) {
            Stop-Process -Id $existing.ProcessId -ErrorAction Stop
            for ($i = 0; $i -lt 30; $i++) {
                if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { break }
                Start-Sleep -Milliseconds 200
            }
            if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
                throw '服务尚未释放端口，请稍后再次停止。'
            }
        }
        Write-Host '学习服务已停止，已保存的学习记录保留。'
        exit 0
    }
    if ($Action -eq 'Status') {
        if ($existing -and (Test-Healthy)) { Write-Host "正在运行：$serviceUrl"; exit 0 }
        throw '服务未启动或需要重启。请双击“打开学习练习.cmd”；若提示版本更新，查看 README.html 的维护说明。'
    }
    if (-not $existing) {
        if (-not (Test-Path -LiteralPath $pythonPath)) {
            throw '未找到项目 Python 环境。请按 README.html 的首次安装说明执行 uv sync --dev。'
        }
        [IO.Directory]::CreateDirectory($logRoot) | Out-Null
        Write-Host '正在启动学习服务，请稍候……'
        Start-Process -FilePath $pythonPath -ArgumentList "-B -m uvicorn app.main:app --host 127.0.0.1 --port $Port" `
            -WorkingDirectory $projectRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $logRoot "server-$Port.log") `
            -RedirectStandardError (Join-Path $logRoot "server-$Port-error.log") | Out-Null
        for ($i = 0; $i -lt 60; $i++) {
            if (Test-Healthy) { break }
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not (Test-Healthy)) {
        throw "服务尚未就绪或代码已更新。请查看 README.html 的维护说明。错误日志：$logRoot\server-$Port-error.log"
    }
    # Recheck ownership after startup, before opening a page on this port.
    $null = Get-LocalService
    Write-Host "学习服务已启动：$serviceUrl"
    if (-not $NoBrowser) { Start-Process $serviceUrl }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
