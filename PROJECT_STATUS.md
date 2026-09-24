# Project Status

## 当前成果

- CourseFlow 提供 B 站公开视频解析与下载、UP 主投稿批量浏览、本地或 MiMo 转写，以及腾讯会议回放解析桥接。
- B 站自动转写按 provider 检查依赖：只有 local 在获取音频前检查 FunASR；MiMo 跳过本地 ASR，继续音频下载、MiMo 队列和云端转写流程。
- B 站二维码显示故障已修复；二维码依赖独立于腾讯会议 Playwright。腾讯会议桥接目前暂停，本轮未修改相关代码。

## 验证

- `python -m unittest discover -s tests -v`：41 项通过，新增 MiMo 本地 ASR 离线及 local 健康检查回归测试。
- `python -m py_compile app.py` 与 `git diff --check` 通过。
- 新增测试使用假下载与假云端转写，不调用真实 MiMo API；真实云端端到端验收待进行。

## 下一步

- 使用 MiMo-V2.6-Flash 完成一次真实 B 站转写，确认云端结果正常；之前的 B 站扫码真实验收也仍待用户确认。
