# Project Status

## 已完成

- 已完成顶部导航重构：品牌、单视频、UP 主批量、服务状态和设置均位于顶部，删除桌面端常驻左侧栏。
- UP 主投稿已改为优先使用 WBI 签名的 `/x/space/wbi/arc/search`，按 `data.page.count/pn/ps` 返回真实总数和页码；标题搜索直接传 `keyword`。
- 已增加投稿数字页码导航、当前页选择、跨页保留勾选、时间筛选结果分页和列表定位。
- 已增加真实“投稿 / 合集和系列”二级入口；合集目录使用 `/x/polymer/web-space/seasons_series_list`，合集视频使用 `/x/polymer/web-space/seasons_archives_list`，系列视频使用 `/x/series/archives`，详情支持分页、选择和批量任务提交。
- 单视频、封面代理、下载目录、MP4/M4A、任务状态和启动器 build 自动更新机制未改动其既有实现。

## 当前

- 目标主页 `mid=24715837` 的合集目录可正常读取，当前接口返回 14 个合集；首个合集 `7828377` 返回 66 个视频、共 3 页。
- UI 已保留 UP 主批量下载入口，投稿和合集详情均复用现有 BBDownNext 任务体系；未开发扫码登录、专栏、字幕、弹幕、4K。
- 投稿时间筛选会由后台读取接口分页结果后计算并缓存，不使用动态流的估算总数。

## 问题

- B 站匿名 `arc/search` 存在间歇性风控：本轮一次成功响应返回 UP 主“祿命闡微”18 个投稿、1 页、18 条；随后同一接口多次返回 `-352/-412` 安全验证。代码现以该接口为主并重试 3 次，失败时显示中文提示，不回退为动态流的错误估算总数。
- 由于投稿接口当前风控，尚未声称真实投稿多页完整性、筛选和批量下载验收通过；合集目录和首个合集详情已完成真实分页去重验证。

## 下一步

- 在 B 站匿名投稿接口恢复可用后，重新验证目标主页投稿的第一页、中间页、最后一页、count 与 BVID 去重总数，并完成浏览器页面上的筛选、跨页勾选和批量 MP4/M4A 下载回归。
- 继续保持不读取用户浏览器 Cookie；若 B 站明确要求登录，再单独评估登录提示方案。

## 本轮验证

- `python -m py_compile app.py`、`node --check static/app.js`、`git diff --check` 通过。
- 隔离 HTTP 实例验证 `/api/health`、`/api/up`、`/api/up/collections`、`/api/up/collection`；`/api/up` 不再返回 404，风控时返回明确中文 400。
- 目标主页合集分页验证：用每页 5 条读取第 1～3 页，接口报告总数 14，去重后得到 14 个合集；首个合集第 1～3 页分别返回 30、30、6 条，报告总数 66，BVID 去重后得到 66 条。
- 目标主页一次成功的投稿接口响应：`count=18`、`pn=1`、`ps=30`、总页数 1、返回 18 条，首条 BVID `BV1r7hC6BEur`；后续请求受 B 站匿名风控影响，未将一次成功响应扩大为完整多页结论。
- `/app.js` 继续返回 `Cache-Control: no-store, must-revalidate, no-cache, max-age=0` 和 `Pragma: no-cache`。
- 测试使用临时隔离目录 `C:\Users\Atlas\AppData\Local\Temp\BilibiliDownloader-up-refactor-http*`，未修改真实 `%APPDATA%\\BilibiliDownloader`。
