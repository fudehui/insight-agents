# 评测报告：baseline-w1

- 生成时间：2026-09-25T22:20:28
- 口径：P95 为最近邻秩（样本 <20 时即最大值）；指标名对齐 docs/evaluation-metric-template.md；评分细节见 evals/scoring 模块 docstring

## 汇总

| 指标 | 值 |
| --- | --- |
| cases | 30（完成 19 / 失败 11） |
| 任务完成率 | 63.3%（19/30） |
| 单任务 Token（合计） | 5891936 |
| 端到端耗时（平均） | 135.3 s |
| 端到端耗时（P95，最近邻秩） | 378.7 s |
| 整体引用准确率 | 45.2%（42/93） |
| 引用覆盖率 | 13.7%（42/306） |
| 忠实度（Faithfulness，judge=agnes-2.5-flash） | 0.9778 |
| 答案相关性（AnswerRelevancy，阈值 0.5） | 0.9738 |

## 每 case 明细

| case_id | status | 耗时(s) | token | 引用准确率 | 忠实度 | 相关性 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| w1-001 | completed | 147.8 | 178334 | — | 1.0000 | 1.0000 |   |
| w1-002 | completed | 176.4 | 173425 | — | 0.9661 | 1.0000 |   |
| w1-003 | completed | 129.2 | 111255 | — | 1.0000 | 0.9153 |   |
| w1-004 | completed | 124.5 | 652054 | — | 1.0000 | 0.9130 |   |
| w1-005 | completed | 110.0 | 148840 | — | 1.0000 | 1.0000 |   |
| w1-006 | completed | 370.2 | 387178 | 50.0%（3/6） | 0.8723 | 0.8519 |   |
| w1-007 | completed | 149.2 | 179331 | 80.0%（4/5） | 1.0000 | 1.0000 |   |
| w1-008 | completed | 402.5 | 371233 | 40.0%（2/5） | 1.0000 | 1.0000 |   |
| w1-009 | completed | 378.7 | 578632 | 41.9%（13/31） | — | 0.9938 |   |
| w1-010 | completed | 65.5 | 223784 | — | — | — | 引用评分跳过：report_md 为空（None 或全空白），无法做引用评分；completed case 不应产出空报告，请检查 run_eval 的报告收集链路 |
| w2-001 | failed | 252.1 | 261430 | — | — | — | 未评分：status=failed；error=执行主智能发生异常信息：Error code: 429 - {'error': {'code': '', 'message': 'You’ve reached  |
| w2-002 | completed | 141.2 | 164313 | — | 0.8471 | 1.0000 |   |
| w2-003 | completed | 157.0 | 156112 | 71.4%（5/7） | — | 0.9355 |   |
| w2-004 | completed | 162.9 | 174578 | — | 1.0000 | 1.0000 |   |
| w2-005 | completed | 124.1 | 145770 | 44.4%（8/18） | 0.9815 | 1.0000 |   |
| w2-006 | completed | 137.1 | 194231 | 36.8%（7/19） | 1.0000 | 1.0000 |   |
| w2-007 | failed | 176.5 | 183976 | — | — | — | 未评分：status=failed；error=执行主智能发生异常信息：Error code: 429 - {'error': {'code': '', 'message': 'You’ve reached  |
| w2-008 | completed | 137.0 | 346411 | — | 1.0000 | 0.9452 |   |
| w2-009 | completed | 76.3 | 124002 | 0.0%（0/1） | 1.0000 | 1.0000 |   |
| w2-010 | failed | 137.5 | 166577 | — | — | — | 未评分：status=failed；error=执行主智能发生异常信息：Error code: 429 - {'error': {'code': '', 'message': 'You’ve reached  |
| w2-011 | completed | 83.5 | 144835 | 0.0%（0/1） | 1.0000 | 1.0000 |   |
| w2-012 | failed | 44.0 | 57695 | — | — | — | 未评分：status=failed；error=执行主智能发生异常信息：Error code: 429 - {'error': {'code': '', 'message': 'You’ve reached  |
| w2-013 | failed | 114.2 | 171279 | — | — | — | 未评分：status=failed；error=执行主智能发生异常信息：Error code: 429 - {'error': {'code': '', 'message': 'You’ve reached  |
| w2-014 | failed | 43.9 | 54288 | — | — | — | 未评分：status=failed；error=执行主智能发生异常信息：Error code: 429 - {'error': {'code': '', 'message': 'You’ve reached  |
| w2-015 | completed | 25.4 | 52233 | — | — | — | 引用评分跳过：report_md 为空（None 或全空白），无法做引用评分；completed case 不应产出空报告，请检查 run_eval 的报告收集链路 |
| w2-016 | failed | 32.6 | 84610 | — | — | — | 未评分：status=failed；error=执行主智能发生异常信息：Error code: 429 - {'error': {'code': '', 'message': 'You’ve reached  |
| w2-017 | failed | 56.2 | 153405 | — | — | — | 未评分：status=failed；error=执行主智能发生异常信息：Error code: 429 - {'error': {'code': '', 'message': 'You’ve reached  |
| w2-018 | failed | 30.7 | 118979 | — | — | — | 未评分：status=failed；error=执行主智能发生异常信息：Error code: 429 - {'error': {'code': '', 'message': 'You’ve reached  |
| w2-019 | failed | 30.4 | 74817 | — | — | — | 未评分：status=failed；error=执行主智能发生异常信息：Error code: 429 - {'error': {'code': '', 'message': 'You’ve reached  |
| w2-020 | failed | 41.8 | 58329 | — | — | — | 未评分：status=failed；error=执行主智能发生异常信息：Error code: 429 - {'error': {'code': '', 'message': 'You’ve reached  |
