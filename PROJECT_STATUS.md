# Project Status

## 当前成果

- 已完成“B 站视频 → 临时音频 → 本地 ASR → 文字稿”的后端闭环；自动转写使用独立临时 BBDown 服务目录，不占用用户下载目录。
- 单视频、UP 主批量、合集/系列详情均新增“转写文字稿”；MP4/M4A 仍进入原下载任务区，转写任务进入“本地转写”多任务区。
- 本地转写区可同时显示多个任务的名称、阶段、进度、成功/失败原因；成功结果可查看全文/时间轴并复制或导出 TXT/SRT/VTT；手工选择本地音频路径保留。
- 多 P 音频按实际发现的音频文件分别建立任务；自动流程结束后清理临时音频，并请求 ASR 服务清理其任务文件。
- 已保留真实登录态和设备 Cookie 会话合并逻辑；日志只记录 Cookie 名称集合，不记录 Cookie 值。
- 已增加统一转写引擎设置：本地 FunASR、`mimo-v2.5-asr`、`mimo-v2.6-flash`；默认本地，任务创建时记录实际 provider/model。
- MiMo API Key 使用独立 Windows DPAPI 文件保存，`config.json` 仅保存引擎选择；设置接口只返回已配置状态和脱敏提示。
- MiMo 适配已按官方 OpenAI Chat Completions 结构实现：v2.5 使用 MP3/WAV、`asr_options.language=zh`；v2.6 使用忠实转写 prompt；两者都支持 FFmpeg 转 MP3、长音频切片、有限退避重试和临时文件清理。
- MiMo 结果不伪造时间戳；无可靠时间戳时仅提供全文/TXT，时间轴和 SRT/VTT 会禁用。
- MiMo Flash 已关闭 thinking，并将单片段 `max_completion_tokens` 提高到 65536；保留现有切片、顺序合并和有限重试流程。
- MiMo 每个片段现在记录 provider/model、片段序号、音频字节数、HTTP 状态、finish_reason、安全 usage 数值、正文/推理正文长度；不记录 API Key、Base64、请求体或转写正文，并拒绝空正文和异常/长度 finish_reason。
- 诊断 ZIP 已改为写入现有 `LOG_DIR`；创建前 flush 日志，创建成功与 Explorer 定位分离，不再直接打开 ZIP 文件。

## 已通过的测试

- `python -m py_compile app.py`、`node --check static/app.js`、`node --check static/asr.js`、`git diff --check`。
- 本地 ASR 服务真实健康检查通过：`ready=true`、`model_loaded=true`、设备 `cuda:0`。
- 单视频 `BV1eQNL6JEV2` 真实自动转写通过：临时 M4A 下载成功，ASR 结果 `succeeded`，返回 27,491 字符、3,050 个时间段；临时目录已清理。
- 目标 UP 主 `24715837` 投稿接口真实返回 18 条、1 页；选取两个投稿分别加入自动转写，两个任务均真实完成。
- 目标 UP 主合集/系列列表返回 14 个，抽样打开一个合集返回 66 个视频、30 条/页、3 页。
- 单视频解析保留分 P 逻辑；实测目标视频返回 1 个分 P。普通音频任务接口仍返回 HTTP 202。
- 设置接口默认本地引擎、缺少 MiMo Key 的明确错误、Key 不写入 `config.json` 已验证；手工本地音频经统一后端任务真实完成，返回 217 字符、5 个时间段，临时目录已清理。
- 使用本地模拟 HTTP 响应验证两个 MiMo provider 的请求结构、v2.5 `language=zh`、v2.6 忠实转写 prompt、音频切片和无时间戳结果；该模拟测试未发起计费云端请求。
- 使用本地模拟响应验证 Flash `thinking.type=disabled`、65536 输出预算、成功正文和空正文失败路径；日志仅输出安全元数据。
- 实测 `/api/export-logs` 创建 ZIP 成功，ZIP 位于 `LOG_DIR` 且可读取，包含当前日志文件；实测 `/api/open-log-directory` 返回成功。

## 未完成 / 尚未验证

- 尚未用真实多 P 视频完成“多个实际音频文件拆成多个转写任务”的端到端样本；代码已按实际音频文件逐个建任务。
- 尚未在浏览器中逐项点击验收所有按钮；接口和前端静态语法已验证。普通 MP4 下载、手工本地音频转写、合集转写需继续做页面级回归。
- 本轮未启用领域热词或错词纠正。
- 已使用当前本机已保存的 MiMo Key 完成受控真实验证：`/models` 认证通过；20 秒 M4A 的 Flash 单片段成功返回 209 字符；约 15 分钟 MP3 实际切为 2 片，两片均 HTTP 200、`finish_reason=stop`，最终任务成功并合并 5729 字符。
- v2.5-ASR 尚未重新发起真实计费请求；此前仅完成请求结构模拟验证。

## 下一步

- 下一步仅需按需补做 v2.5-ASR 的一次短音频真实 A/B 请求，以及页面级按钮回归；Flash 长音频问题已通过本轮真实复测。
