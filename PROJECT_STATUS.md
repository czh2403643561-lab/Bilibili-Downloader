# Project Status

## 已完成

- 新增 `browser-extension/` MV3 CourseFlow Meeting Bridge；本地任务、随机 bridge token 与敏感媒体请求上下文只保存在当前进程/扩展内存，任务结果在 30 分钟后过期。
- CourseFlow 提供桥接状态、配对、heartbeat、串行领取、进度、结果及安全任务状态 API；严格限制腾讯会议 `/cw/`、`/crm/` URL，敏感媒体 URL/headers 不返回页面、不写日志或配置。
- 扩展使用浏览器现有登录态，单次仅处理一个后台 `active:false` worker tab；媒体采集按 task/tab/navigation generation 隔离，捕获唯一 MP4 请求上下文后回传，并在成功/失败时关闭 tab、清理内存。
- 腾讯会议页面改走 bridge，未连接时禁用解析；设置页显示插件状态。旧 Playwright 路线保留但已停用为默认 UI 流程。
- README 已改为桥接插件方案；B站下载和转写未改。

## 验证

- Python 编译及 `python -m unittest discover -s tests -v`：23 项通过；桥接 Node 测试：9 项通过；前后端 `node --check` 与 `git diff --check` 通过。
- 本地 API smoke test 通过：正确扩展来源/token、CORS 预检、heartbeat、单次 claim、阶段更新及结果回传；错误来源/token 被拒绝，页面状态不含媒体 URL/Cookie；测试日志未发现媒体签名/凭据标记。
- 隔离 Chrome 页面运行 CourseFlow：无 JavaScript 页面异常，未连接扩展时解析按钮保持禁用并显示未连接提示。
- Playwright headless 环境未能加载扩展，因此未把模拟 worker 测试当作真实扩展验收。

## 未完成

- 尚未用用户真实登录的 Chrome/Edge 和有权限回放验证扩展实际加载、后台 tab、真实 MP4 捕获、回传及 tab 关闭；本阶段未下载媒体。
- 当前媒体 host 权限限于腾讯相关域名；真实回放若使用其它 CDN，需根据实际请求补充最小域名权限。
- `meeting_browser.py` 旧 Playwright 实现仍保留，等待后续清理。

## 下一步

- 在 `chrome://extensions` 或 `edge://extensions` 打开开发者模式，加载仓库 `browser-extension/`；先在该浏览器正常登录腾讯会议，再从 CourseFlow 检查插件并解析一条有权限的回放。
- 只验收到页面显示解析成功、课程标题和“已找到视频资源”，确认后台 tab 关闭后停止；不测试下载/转写。
