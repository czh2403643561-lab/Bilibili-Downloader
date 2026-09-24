# Project Status

## 当前成果

- B 站登录使用 DPAPI 加密的统一会话，账号与设备 Cookie 合并请求；日志只记录 Cookie 名称，不记录值。
- 用户已人工确认当前 B 站核心流程可继续使用；本轮未改变下载核心、登录、任务队列或下载目录逻辑。
- 合集/系列卡片优先使用列表响应中 `archives[0].pic` 的首视频封面，14 个目标合集均已取得封面；仍经 `/api/cover` 本地代理加载，无需逐个额外请求。
- 顶部新增“本地转写”页面，定位为“B站媒体获取 + 本地 AI 转写客户端”。
- 新增独立 `static/asr.js`：定义 AsrJob、AsrSegment、AsrResult 的统一 Schema，提供 `checkHealth`、创建任务、查询任务/结果、取消任务的 Adapter 边界；ASR Base URL 仅在该文件内配置，当前为空。
- 已完成本地音频 Mock 流程：M4A/MP3/WAV/FLAC 文件信息、准备音频、上传、排队、转写、完成、取消与失败测试路径；页面明确显示“模拟模式 / 等待真实 ASR 服务”，不伪造百分比或 AI 推理结果。
- 文字稿支持全文/时间轴、复制全文、TXT/SRT/VTT 导出；时间异常时禁用或拒绝字幕导出，不伪造时间戳。

## 已通过的测试

- `python -m py_compile app.py`、`node --check static/app.js`、`node --check static/asr.js`、`git diff --check`。
- Mock Adapter：完整完成链路、queued 阶段取消、failed 测试路径、TXT/SRT/VTT 格式与非法字幕时间校验通过。
- 本地启动器启动后 `/api/health` 返回版本一致；浏览器已验证“本地转写”导航、Mock 状态提示及未选文件时按钮禁用。
- `/api/up/collections` 实测 14 项均含首视频封面。

## 未完成

- 真实 ASR 服务已独立并入 `asr-service/`，本仓库客户端仍不直接加载 FunASR、Paraformer、CUDA 或模型文件。
- 已用真实本地音频验证服务 API；客户端尚未完成真实上传 Adapter 联调，后续需回归现有文字稿 UI。

## 下一步

- 按 `docs/ASR_HANDOFF.md` 的 Base URL 和 POST `/v1/jobs` 契约接入真实 Adapter，并回归现有文字稿 UI。

## ASR 模块

- ASR 服务代码已并入独立目录 `asr-service/`，不加载到 B 站下载器的 `app.py` 或 `static/`。
- 当前 API 已支持本地异步上传、SQLite 任务状态、单 GPU 队列、Paraformer 中文 ASR、FSMN-VAD、标点、时间戳、句段和官方热词字段。
- 当前模型为 FunASR `1.4.16` + Paraformer/VAD/标点模型，已在 RTX 5060 GPU 上完成真实课程音频测试；当前没有 Speaker 模型。
- ASR 服务可以独立启动和调用；尚未完成与 Bilibili Downloader 客户端的真实音频联调。
