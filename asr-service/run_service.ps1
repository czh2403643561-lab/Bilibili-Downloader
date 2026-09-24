$ErrorActionPreference = "Stop"

$serviceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = if ($env:ASR_PYTHON) { $env:ASR_PYTHON } else { "C:\AI\FunASR.venv\Scripts\python.exe" }
$hostName = if ($env:ASR_HOST) { $env:ASR_HOST } else { "127.0.0.1" }
$port = if ($env:ASR_PORT) { $env:ASR_PORT } else { "8765" }

if (-not (Test-Path -LiteralPath $python)) {
    throw "找不到 ASR Python 环境：$python。请设置 ASR_PYTHON 指向 Python 3.11 虚拟环境。"
}

& $python -m uvicorn asr_service.app:app `
    --app-dir $serviceRoot `
    --host $hostName `
    --port $port `
    --log-level info
