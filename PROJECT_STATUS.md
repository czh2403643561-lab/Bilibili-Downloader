# Project Status

## 当前成果

- CourseFlow 提供 B 站公开视频解析与下载、UP 主投稿批量浏览、本地或 MiMo 转写，以及腾讯会议回放解析桥接。
- MiMo-V2.6-Flash 音频片段遇到 `content_filter` 时，仅对失败片段按约 300 秒、150 秒逐级缩小重试；已完成片段保留，耗尽重试后明确报错并释放串行队列。
- B 站自动转写按 provider 检查依赖：只有 local 在获取音频前检查 FunASR；MiMo 跳过本地 ASR，继续音频下载、MiMo 队列和云端转写流程。
- B 站二维码显示故障已修复；腾讯会议桥接目前暂停，本轮未修改相关代码。

## 验证

- `python -m unittest discover -s tests -v`：45 项通过，包含 MiMo 正常完成、内容过滤缩片段恢复、失败后下一任务继续和队列串行回归。
- `python -m py_compile app.py tests/test_mimo_content_filter.py` 与 `git diff --check` 通过。
- 测试使用假云端响应，不调用真实 MiMo API；真实云端转写验收待进行。

## 下一步

- 使用 MiMo-V2.6-Flash 完成真实 B 站转写，确认云端结果及内容过滤后的恢复表现；B 站扫码真实验收也仍待确认。
