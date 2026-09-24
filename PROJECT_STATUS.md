# Project Status

# CourseFlow ↔ Meeting Bridge

## 已完成

- 修复配对对 `Origin` 的单点依赖：固定扩展 ID + 当前 CourseFlow 进程随机 token 双重校验；`Origin` 缺失可用，存在时必须匹配该 Chrome/Edge 扩展，普通网页来源拒绝。
- MV3 扩展把非敏感诊断状态保存在 `chrome.storage.session`，区分服务不可达、CORS/配对失败、鉴权失败、心跳失败和后台未启动；popup 提供重新连接、打开 CourseFlow 与折叠诊断信息。
- 心跳由 `chrome.alarms` 驱动，并在扩展启动/安装时重新调度；popup 关闭不影响后台桥接。设置页提示插件内重新连接；“检查插件”只查询当前服务状态。
- 未修改回放解析、媒体捕获、下载、音频或转写逻辑。

## 验证

- Python 桥接单元与本机 HTTP 路由测试：18 项通过，含无 Origin 配对/心跳、错误 ID/token、恶意 Origin、CORS 预检及 token 不入日志。
- 扩展 Node 测试：18 项通过，含配对/网络/CORS/鉴权诊断、自动重新配对、手动重连、alarm/startup/install 调度和 popup 状态。
- 等待用户在 Chrome/Edge 重载扩展并点击“重新连接”验证真实服务 worker 通信；本轮没有测试腾讯会议回放或媒体。

## 下一步

- 启动 CourseFlow，在 `chrome://extensions` 重载 CourseFlow Bridge，打开插件并点“重新连接”；确认 popup 与设置页显示已连接。关闭 popup 等待 60 秒，再打开检查连接仍正常。
