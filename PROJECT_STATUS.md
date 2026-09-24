# Project Status

## 当前成果

- 启动器与 `app.py` 共用 `build_info.py` 的构建文件清单和 `current_build_id()`；包含 `static/asr.js`，不包含 `asr-service`。
- 启动器继续使用无控制台的 VBS 入口；旧实例会按项目健康接口安全退出后再启动新版本。
- `app.py` 启动失败时，子进程 stdout/stderr 保存在 `%APPDATA%\\BilibiliDownloader\\logs\\app-startup.log`，启动器日志只记录路径和状态，不吞掉真实 traceback。
- 客户端已接入真实 ASR Adapter：固定 Base URL 为 `http://127.0.0.1:8765`，使用 `/health`、`POST /v1/jobs`、任务查询、结果、`POST /cancel` 和终态清理；上传使用原生 `FormData`，不手写 Content-Type。
- CORS 默认仅允许 `http://127.0.0.1:23666` 和 `http://localhost:23666`。
- `asr-service` 默认使用 `C:\\AI\\FunASR.venv\\Scripts\\python.exe`；引擎优先使用本机 ModelScope 缓存快照，避免代理配置导致已缓存模型无法启动。
- Mock 仅在 URL `?asr=mock` 或 `localStorage.asr-dev-mode=mock` 下启用；普通离线状态不会伪造转写结果。

## 已通过的测试

- `python -m py_compile app.py 启动工具.pyw asr-service/asr_service/config.py`。
- `node --check static/app.js`、`node --check static/asr.js`。
- 构建 ID 一致性、VBS 启动、旧实例替换、`static/asr.js` 改动检测通过。
- 人为启动异常测试通过：`app-startup.log` 保留了真实 `RuntimeError` traceback。
- 默认 ASR 服务真实启动：`GET /health` 返回 200，`ready=true`，`model_loaded=true`，设备为 `cuda:0`。
- 真实文件 `C:\\Users\\Atlas\\Downloads\\新录音 12.m4a` 已通过 API 和页面端到端验证：POST 202；状态经历 `processing 5% -> 95% -> succeeded 100%`；结果含 217 字符文本、5 个句段、38 个时间戳；页面全文和时间轴均可见。
- Mock 的完整、取消、失败及 TXT/SRT/VTT 格式校验仍通过既有测试。

## 未完成 / 尚未验证

- 当前只完成“本地音频 -> ASR”链路，尚未把 B 站下载后的音频自动串入 ASR。
- 浏览器自动下载目录中的导出文件落盘路径尚未单独核验；导出函数和字幕时间校验已通过。
- 本轮未改变 B 站解析、登录、UP 主批量、合集/系列和下载任务逻辑；这些路径需在下一轮按现有项目验收清单回归。

## 下一步

- 先回归单视频、UP 主批量、合集/系列和下载任务；确认稳定后再设计 B 站媒体到 ASR 的连接，不在当前客户端隐藏自动串联逻辑。
