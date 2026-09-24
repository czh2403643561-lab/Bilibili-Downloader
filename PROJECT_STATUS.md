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

- 真实 ASR 服务、上传 body 与最终端口尚未提供，因此未接入模型或推理服务；本仓库不会加载 FunASR、Paraformer、CUDA 或模型文件。
- 本轮未用真实音频提交至真实 ASR 服务；未来接入时需按服务契约实现上传 Adapter，并回归现有文字稿 UI。

## 下一步

- 等待独立 ASR 服务提供 Base URL、认证方式（如有）和 POST `/v1/jobs` 的请求/响应规范后接入真实 Adapter。
