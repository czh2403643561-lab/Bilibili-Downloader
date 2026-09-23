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

if (-not (Test-Path (Join-Path $projectRoot 'tools\BBDownNext\BBDown.exe'))) {
    Show-Problem '缺少 BBDownNext。请先右键“安装依赖.ps1”，选择“使用 PowerShell 运行”。'
    exit 1
}

Start-Process -FilePath $pythonw.Source -ArgumentList ('"' + (Join-Path $projectRoot 'app.py') + '" --open-browser') -WorkingDirectory $projectRoot -WindowStyle Hidden
