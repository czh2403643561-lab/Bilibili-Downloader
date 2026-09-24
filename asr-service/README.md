# 本地 ASR 服务

这是独立的 FunASR HTTP 服务层。服务启动时加载一次 Paraformer、FSMN-VAD 和标点模型；GPU 推理由单个队列 worker 串行执行，任务记录保存在 SQLite。它不依赖 B 站下载器页面。

## 启动

在 PowerShell 中，从仓库根目录执行：

```powershell
$env:ASR_PYTHON = "C:\AI\FunASR.venv\Scripts\python.exe"
.\asr-service\run_service.ps1
```

`ASR_PYTHON` 应指向已经安装 FunASR、PyTorch GPU 和模型依赖的 Python 3.11 虚拟环境。默认监听 `http://127.0.0.1:8765`；如需局域网访问，可临时设置 `ASR_HOST=0.0.0.0`。

模型文件不提交 Git。首次运行时使用现有 FunASR/ModelScope 模型缓存；若缓存不存在，按当前 FunASR 文档准备模型后再启动服务。必要时只在当前 PowerShell 临时设置下载代理，不修改系统配置。

## 最小调用

```powershell
$created = curl.exe --noproxy "*" -sS `
  -F "file=@C:/path/to/audio.mp3" `
  -F 'hotwords=["食神","伤官","月令"]' `
  http://127.0.0.1:8765/v1/jobs | ConvertFrom-Json

Invoke-RestMethod "http://127.0.0.1:8765/v1/jobs/$($created.task_id)"
Invoke-RestMethod "http://127.0.0.1:8765/v1/jobs/$($created.task_id)/result"
```

接口返回结构、状态流和当前已验证能力见 [`docs/ASR_HANDOFF.md`](../docs/ASR_HANDOFF.md)。
