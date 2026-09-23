param([switch]$Quiet)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$tools = Join-Path $projectRoot 'tools'
$bbdownPath = Join-Path $tools 'BBDownNext\BBDown.exe'
$ffmpegPath = Join-Path $tools 'ffmpeg\bin\ffmpeg.exe'
New-Item -ItemType Directory -Force -Path (Split-Path $bbdownPath), (Split-Path $ffmpegPath) | Out-Null

if (-not (Test-Path $bbdownPath)) {
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/KaiHuaDou/BBDownNext/releases/download/v2.2.0/BBDown-win-x64.exe' -OutFile $bbdownPath
}

if (-not (Test-Path $ffmpegPath)) {
    $zipPath = Join-Path $tools 'ffmpeg-download.zip'
    $extractPath = Join-Path $tools 'ffmpeg-extract'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
    $source = Get-ChildItem -Path $extractPath -Directory | Select-Object -First 1
    Move-Item -Path (Join-Path $source.FullName 'bin\*') -Destination (Split-Path $ffmpegPath) -Force
    Remove-Item -LiteralPath $zipPath, $extractPath -Recurse -Force
}

if (-not $Quiet) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show('依赖已安装。现在可双击“启动工具.vbs”启动。', 'Bilibili Downloader') | Out-Null
}
