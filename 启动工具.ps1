$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue

function Show-Problem([string]$message) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($message, 'Bilibili Downloader') | Out-Null
}

if (-not $pythonw) {
    Show-Problem '未找到 Python。请安装 Python 3.12 或更高版本，然后重新双击“启动工具.vbs”。'
    exit 1
}

try {
    & (Join-Path $projectRoot '安装依赖.ps1') -Quiet
} catch {
    Show-Problem ('自动准备下载组件失败：' + $_.Exception.Message + '。请检查网络后重新双击“启动工具.vbs”。')
    exit 1
}

$instanceId = [Guid]::NewGuid().ToString('N')
$arguments = '"' + (Join-Path $projectRoot 'app.py') + '" --instance-id ' + $instanceId
Start-Process -FilePath $pythonw.Source -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden

for ($attempt = 0; $attempt -lt 80; $attempt++) {
    try {
        $request = [System.Net.WebRequest]::Create('http://127.0.0.1:23666/api/health')
        $request.Proxy = $null
        $request.Timeout = 500
        $response = $request.GetResponse()
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        $health = $reader.ReadToEnd() | ConvertFrom-Json
        $reader.Dispose()
        $response.Dispose()
        if ($health.instance_id -eq $instanceId) {
            Start-Process 'http://127.0.0.1:23666/'
            exit 0
        }
    } catch {}
    Start-Sleep -Milliseconds 250
}

Show-Problem '本地服务未能正常启动，因此没有打开页面。请重新双击“启动工具.vbs”；若仍失败，请检查诊断日志。'
