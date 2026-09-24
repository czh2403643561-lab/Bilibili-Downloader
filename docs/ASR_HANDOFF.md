# ASR 服务交接文档

## ASR 服务是什么

ASR Service 是本机/局域网使用的 HTTP 语音转文字服务。它接收音频，排队执行 FunASR 推理，并返回结构化 JSON、全文、句段和原始时间戳。

当前主要组件为 Paraformer 中文 ASR、FSMN-VAD 和 CT-Transformer 中英文标点模型。服务只负责音频推理及其结果封装，不负责 B 站下载、网页 UI 或客户端业务流程。

## 当前运行环境

- Windows PowerShell 环境，当前测试 Build 为 `28000`。
- Python `3.11.9`，使用独立虚拟环境；不改动仓库客户端使用的 Python 3.12。
- NVIDIA GeForce RTX 5060，显存约 8 GB；驱动 `610.62`。
- PyTorch `2.11.0+cu130`，内置 CUDA runtime `13.0`；torchaudio `2.11.0+cu130`。
- FunASR `1.4.16`，ffmpeg/ffprobe 可用。
- 不要求单独安装 CUDA Toolkit 或 cuDNN；服务使用 PyTorch wheel 自带 runtime。
- 默认启动：`asr-service/run_service.ps1`；默认监听 `http://127.0.0.1:8765`。
- 模型文件、缓存、SQLite、上传音频和结果文件均在本地运行时生成，不提交 Git。当前模型通过 FunASR/ModelScope 缓存准备。

## 当前模型能力

已实际确认的配置：

- ASR：`iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch`。
- VAD：`iic/speech_fsmn_vad_zh-cn-16k-common-pytorch`，已启用。
- 标点：`iic/punc_ct-transformer_cn-en-common-vocab471067-large`，已启用。
- 时间戳：已启用；保留模型返回的 raw token/字符级 `timestamps`，并根据实际 `sentence_info` 聚合 `segments`。
- Speaker：当前没有说话人模型；`speaker` 字段保留但统一为 `null`。
- 热词：已接入 FunASR/Paraformer 官方模型级 `hotword` 参数，不使用识别后字符串替换。
- 中文普通话：已用真实课程音频验证可运行；专业八字词仍可能误识别，热词效果需要按具体课程继续评估。
- 英文和中英混合：当前没有单独完成可靠基准测试；ASR 主模型是中文模型，因此英文/混合内容不承诺与中文同等准确率。标点模型名称包含中英文，但不等于 ASR 主模型具备完整多语能力。

## 当前 API

Base URL：`http://127.0.0.1:8765`。默认端口：`8765`。

状态枚举：`queued`、`processing`、`succeeded`、`failed`、`cancelled`。`progress` 存在，范围为 `0-100`。

### `GET /health`

```json
{
  "api_version": "v1",
  "api_ok": true,
  "ready": true,
  "model_loaded": true,
  "model_load_error": null,
  "device": "cuda:0",
  "gpu_available": true,
  "queue_length": 0,
  "current_task_id": null,
  "model_name": "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
  "service_version": "0.1.0"
}
```

### `POST /v1/jobs`

- Content-Type：`multipart/form-data`。
- 文件字段：`file`。
- 可选字段：`hotwords`，JSON 字符串数组；最多 500 个热词。
- 当前支持扩展名：`.mp3`、`.wav`、`.m4a`、`.mp4`、`.flac`、`.ogg`、`.opus`、`.aac`、`.wma`、`.webm`。
- 默认大小限制：2 GiB。
- 上传成功立即返回 HTTP `202`，实际 GPU 推理异步执行。

```json
{
  "api_version": "v1",
  "task_id": "example-task-id",
  "status": "queued",
  "progress": 0,
  "filename": "course.mp3",
  "text": null,
  "segments": [],
  "timestamps": [],
  "model": null,
  "metrics": null,
  "error": null
}
```

### `GET /v1/jobs/{task_id}`

```json
{
  "api_version": "v1",
  "task_id": "example-task-id",
  "status": "processing",
  "progress": 42,
  "filename": "course.mp3",
  "text": null,
  "segments": [],
  "timestamps": [],
  "model": null,
  "metrics": null,
  "error": null
}
```

### `GET /v1/jobs/{task_id}/result`

任务成功后返回与任务查询相同的稳定外层结构，并包含 `text`、`segments`、`timestamps`、`model` 和 `metrics`。任务未成功时返回 HTTP `409`。

```json
{
  "api_version": "v1",
  "task_id": "example-task-id",
  "status": "succeeded",
  "progress": 100,
  "filename": "course.mp3",
  "text": "这一段是完整识别文字。",
  "segments": [
    {"start_ms": 1200, "end_ms": 5600, "text": "这一段是完整识别文字。", "speaker": null}
  ],
  "timestamps": [[1200, 1450]],
  "model": {"device": "cuda:0", "speaker_enabled": false, "hotwords_enabled": false},
  "metrics": {"audio_seconds": 10.0, "inference_seconds": 0.2, "total_seconds": 0.3, "rtf": 33.3},
  "error": null
}
```

### `DELETE /v1/jobs/{task_id}`

任务已完成、失败或取消后可删除运行时文件；数据库记录保留并标记为 `cancelled`，成功返回 HTTP `204` 且无响应正文。队列中或正在处理的任务返回 HTTP `409`。另有 `POST /v1/jobs/{task_id}/cancel` 用于取消排队任务。

错误通常使用 `{"detail":"..."}`；任务失败时使用：

```json
{
  "error": {
    "code": "INFERENCE_FAILED",
    "message": "实际错误信息"
  }
}
```

## Result Schema

成功结果的主要字段如下：

- `text`：完整标点文字稿。
- `segments`：实际句段数组，每项为 `start_ms`、`end_ms`、`text`、`speaker`。
- `timestamps`：模型返回的 raw token/字符时间戳，单位毫秒。
- `model`：ASR、VAD、标点模型 ID，device，GPU，热词和 speaker 能力标记。
- `metrics`：`audio_seconds`、`model_load_seconds`、`decode_seconds`、`inference_seconds`、`total_seconds`、`rtf`、`peak_allocated_mib`、`peak_reserved_mib`。

`segments` 优先使用 FunASR 的实际 `sentence_info` 和时间戳；没有说话人模型时 `speaker` 为 `null`，不伪造说话人信息。

## 任务状态流

正常流程：`queued → processing → succeeded`。

异常流程：`processing → failed`；排队任务可变为 `cancelled`。服务重启时，数据库中原来处于 `queued` 或 `processing` 的任务会标记为 `failed`，错误码为 `SERVICE_RESTARTED`，历史记录不会丢失，但当前不会自动恢复执行。

## 当前性能基线

已实际测试一段约 `940.014` 秒的真实八字课程音频：

- GPU：NVIDIA GeForce RTX 5060 8 GB。
- `processing_seconds`（接口 `metrics.total_seconds`）：约 `9.821` 秒。
- ASR `inference_seconds`：约 `9.318` 秒。
- RTF：约 `100.88x`，计算方式为音频秒数除以处理秒数。
- PyTorch 峰值显存：约 `2890 MiB` allocated、`3382 MiB` reserved。

该基线是当前单任务 GPU 推理结果，不代表并发吞吐量。

## 已知问题

- 中文专业术语仍有同音/近音错误，例如食神、月令、印绶、倒食、枭劫、生克、伤官、甲辰等词需要继续通过热词和纠错词典评估。
- 中文主模型对英文和中英混合内容尚未做独立可靠基准。
- 没有 Speaker diarization；所有 `speaker` 当前为 `null`。
- 长音频已完成约 15 分钟稳定性验证；更长课程仍应在真实批量场景继续观察。
- VAD 和句段时间戳依赖模型返回的 `sentence_info`；当前不伪造缺失时间戳。
- SQLite 持久化任务记录，但服务重启不会自动恢复未完成任务。
- 模型首次加载/下载可能受本机代理环境影响；模型缓存和运行数据不属于 Git 交付物。

## 下一步模型优化方向

- 在同一段课程上继续比较原始识别与官方热词机制的客观差异。
- 根据真实错误扩充八字专业词库，再单独评估文本纠错词典；不在 ASR 服务层偷偷替换原始结果。
- 如有明确需求，再增加独立 Speaker 模型和对应字段填充。

## 与 Bilibili Downloader 的集成边界

Bilibili Downloader 负责：

- 获取视频/音频。
- 上传音频给 ASR。
- 展示任务状态。
- 展示文字稿。
- TXT/SRT/VTT 导出。

ASR Service 负责：

- 音频接收。
- VAD、ASR、标点和时间戳。
- 热词及后续 Speaker 能力。
- 模型加载、推理和模型质量优化。

客户端只依赖稳定的 HTTP API 和结果字段，不依赖 FunASR 的内部模型实现。
