$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $pythonw) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show('未找到 Python。请安装 Python 3.12 或更高版本，然后双击“启动工具.vbs”。', 'Bilibili Downloader') | Out-Null
    exit 1
}
Start-Process -FilePath $pythonw.Source -ArgumentList ('"' + (Join-Path $projectRoot '启动工具.pyw') + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden
