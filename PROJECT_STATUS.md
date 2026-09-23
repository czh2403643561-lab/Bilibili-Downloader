# Project Status

## 已完成

- 已初始化本地 Git，并连接 GitHub `origin/main`
- 已完成项目定义、基础框架、静默启动、依赖检测、配置保存和脱敏日志
- 已接入 BBDownNext，单视频解析、多 P 选择、MP4/M4A 下载、任务状态、取消、失败重试和打开下载目录可用
- 真实 Windows 用户已确认：双击 `启动工具.vbs` 可启动页面；目录选择与保存成功；单视频解析成功；封面正常；视频实际下载成功；打开下载目录正常
- 已实现 UP 主批量第一版：支持主页链接或 mid、UP 主资料、投稿列表、公开视频封面、发布时间、时长、标题搜索、时间筛选、分页、跨页保留勾选、清空选择和批量加入任务
- UP 主批量投稿默认以 `all` 分 P 提交，不静默只下载 P1；继续复用现有 BBDownNext 任务区和并发限制
- 已修复代码更新后启动器错误复用旧后台的问题：启动器与后台使用 `app.py + 核心静态文件` 自动 hash；版本不一致时会先 shutdown，旧版本不支持时仅结束已确认监听 23666 的项目进程
- 本地 HTML、JS、CSS 已设置 no-cache/no-store；后台 `/api/health` 返回 `build_id`

## 当前

- 投稿列表使用 B 站公开资料接口和空间动态接口，不读取或导入用户浏览器 Cookie；仅准备匿名访问参数
- 已复用 `/api/cover` 同源封面代理和占位图机制
- UI 已增加“UP 主批量”完整入口；本轮未开发扫码登录、专栏、字幕、弹幕、4K
- 当前运行实例已确认是最新 build，启动器二次运行会复用同版本实例

## 问题

- B 站匿名投稿接口存在风控波动：同一公开主页偶尔返回 `code=0` 空列表或安全验证；代码已增加 WBI/匿名参数、无签名兼容回退，并向用户显示中文重试提示，不把有投稿的主页静默显示为空
- B 站匿名投稿接口仍可能返回少于 30 条或要求稍后重试；这与本地接口版本一致性是两个独立问题
- 本轮验证了真实 HTTP 接口和静态资源缓存头，尚未替代用户在真实 Windows 浏览器中完成 UP 页面点击、翻页和勾选操作

## 下一步

- 在真实 Windows 页面用主页 `https://space.bilibili.com/24715837` 验证筛选、翻页、跨页勾选和批量提交
- 继续观察 B 站匿名投稿接口风控；若持续要求登录，再评估提示用户扫码登录（不导入浏览器 Cookie）

## 本轮验证

- `python -m py_compile app.py`、`node --check static/app.js`、`git diff --check` 通过
- 模拟旧实例：启动器发现 23666 的旧项目实例没有 `build_id`，自动结束确认的监听 PID 并启动新实例；新 `/api/health` 返回 build `3257e4307dc00100`
- 再次运行启动器复用同一新实例，实例 ID 未变化；未重复启动后台
- 新实例 `/api/up` 不再返回 404；真实主页返回 UP 主“祿命闡微”、总投稿数 226，本次返回 28 条真实投稿
- 首条投稿字段验证通过：BVID `BV1r7hC6BEur`、标题、发布时间 `2026-09-22`、时长 `44:22`；封面代理 HTTP 200 `image/jpeg`
- 单视频接口回归通过：`BV1qt4y1X7TW` 返回标题、UP 主和 1 个分 P；`/api/tasks` 返回 HTTP 200
- `/app.js` 返回 `Cache-Control: no-store, no-cache, must-revalidate, max-age=0`、`Pragma: no-cache`
- 隔离配置和下载目录：`C:\Users\Atlas\AppData\Local\Temp\BilibiliDownloader-up-http-test-3`，未修改真实 `%APPDATA%\\BilibiliDownloader`
- 使用两个公开视频 `BV1qt4y1X7TW`、`BV1GJ411x7h7` 通过批量任务接口实际完成 MP4 和 M4A 下载；四个文件均存在且非空，任务最终为 `Finished/isSuccessful=true`
- 隔离 `app.log` 未发现新的未处理 traceback；B 站接口风控返回已记录为普通中文 400 提示
