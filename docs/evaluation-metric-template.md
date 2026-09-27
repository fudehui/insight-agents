# 项目评测指标

> 适用范围：「慧研」（insight-agents）。当前阶段只跟踪以下 6 个指标，全部可从 `events.jsonl` + 产物文件自动计算。

## 指标定义

| # | 指标 | 怎么算 | 数据来源 | 目标值 |
| --- | --- | --- | --- | --- |
| 1 | 任务完成率 | 正常产出报告（有 `task_result` 且生成 MD/PDF 文件）的任务数 ÷ 总任务数 | 事件流 + `task_files` | ≥95% |
| 2 | 数值准确率（DB 任务） | 报告中数据库数值与真值一致的个数 ÷ 报告引用的数值总数 | 真值核对脚本 | 100% |
| 3 | 引用覆盖率 | 报告实际引用的来源数 ÷ 登记来源数 | `task_sources` 事件 + 报告正文抽取 | ≥70% |
| 4 | 单任务 Token | input/output/total 求和，看 P50/P95 | `token_usage` 事件累加 | 先跑基线 |
| 5 | 端到端耗时 | `task_start` → `task_result`，看 P50/P95 | 事件时间戳 | 先跑基线 |
| 6 | 重复调用率 | 被指纹拦截（`blocked`）次数 ÷ 总调用尝试 | `tool_end` 事件 | ≤10% |

## 使用方式

1. 写一个脚本逐会话解析 `app/output/session_{thread_id}/events.jsonl` 与产物文件，指标 1、3、4、5、6 直接出数。
2. 指标 2 需要先建约 10 条数据库类评测用例，每条标注真值 SQL + 期望值，例如：

   | 用例 | 任务 | 真值 SQL | 期望值 |
   | --- | --- | --- | --- |
   | TC-DB-001 | 查布洛芬库存并出报告 | `SELECT SUM(quantity_on_hand) FROM inventory WHERE drug_id=(SELECT drug_id FROM drugs WHERE generic_name LIKE '布洛芬%')` | 47000 |
   | TC-DB-002 | 连花清瘟 2025 年全年销售额 | `SELECT SUM(total_amount) FROM sales_records WHERE drug_id=10` | 1200000 |

3. 跑批时机：每次改提示词、换模型或调 `TOOL_BUDGET_JSON` 后全量跑一遍，与上次结果对比。

## 历次评测记录

| 日期/版本 | 完成率 | 数值准确率 | 引用覆盖率 | Token P50/P95 | 耗时 P50/P95 | 重复调用率 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-09-04 / 工作区（基于 d0f6eed，含未提交改动） | 100%（8/8 任务） | 83%（5/6，唯一偏差：报告称销量 TOP10 占比"约50%"，真值 60%） | 78%（35/45，报告 36 个引用 URL 中 35 个在登记来源内） | 15.2万 / 29.3万 | 2.3min / 3.0min | 0% | 基线：`app/output` 既有 3 个会话共 8 个任务全部完成；Token/耗时/重复调用率按其中 2 个含完整埋点的会话（5 任务）计，旧会话（3 任务）缺埋点仅计入完成率。15 次 blocked 全部为 internet_search 预算触顶（上限 5 次/任务），触顶率 15/50=30%，非重复调用 |
| 100 对话量级 | 92–98% | 80–90% | 70–85% | 10–18万 / 30–45万 | 2–4min / 5–8min | 2–8% | 依据：任务类型多样化（DB/网络/多来源/附件）拉大 Token 与耗时分布、多来源任务显著更重；外部服务（Tavily/RAGFlow/LLM）抖动使少量任务失败或降级收尾；DB 数值随聚合复杂度上升出现个别偏差；零星重复调用开始出现。 |

> 后续如需扩展更完整的指标体系（结果质量分层、LLM-as-judge、安全测试等），参考 `docs/agent-optimization-recommendations.md` P1-4 及项目评估方案讨论。

## DeepEval 指标映射（W3 新增，evals/deepeval_runner.py）

在 6 指标之上叠加 LLM-as-Judge 维度（升级计划 P0-1：judge 用低价模型控成本）：

| DeepEval 指标 | 输入 | 衡量什么 | 与 6 指标的关系 |
| --- | --- | --- | --- |
| Faithfulness（忠实度） | input=query、actual_output=report_md、retrieval_context=登记来源拼接 | 报告结论是否被登记来源支撑（幻觉检测） | 引用覆盖率（指标 3）只看"引没引"，本指标看"引得对不对" |
| AnswerRelevancy（答案相关性） | input=query、actual_output=report_md | 报告是否切题回答了任务问题 | 6 指标无对应维度，纯增量 |

配置口径：judge 模型取 `EVAL_JUDGE_MODEL`（未设置回退 `LLM_QWEN_MAX` 同档），阈值默认 0.5（`EVAL_JUDGE_THRESHOLD` 可调）；评分结果落 `evals/results/{run_id}/deepeval.json`，支持增量合并（429 冷却后补跑不重复烧 judge token）。**成本口径**：每 case 两个指标约 8-15 次 judge 调用，30 条全量一轮约 1-3 万 judge token——与业务跑批（240 万/窗口）相比可忽略，但仍建议与跑批共用配额窗口规划。

## 故障注入演练指标（W3 新增、W4 补齐 4 类，evals/fault_inject/）

升级计划 P0-1 的"4 类故障 ×10 次"演练量化口径（前 2 类 W3 落地，后 2 类随 W4 接入）：

| 指标 | 怎么算 | 说明 |
| --- | --- | --- |
| 事件补齐率 | 演练结束后 events.jsonl 的 seq 连续性：`去重条数 ÷ (max-min+1)`，无缺口无重复=1.0 | 事件已落盘即视为可经断线回放补齐（复用系统既有机制）；seq 跨进程续接（monitor 按落盘行数初始化基数），进程重启本身不产生缺口 |
| 恢复成功率 | 注入故障后任务仍按预期收尾的次数 ÷ 总次数 | 断网：任务 completed 且 call_guard 重试真实发生；审批中重启：新进程 resume 后 `task_result` 产出；WS 断连：任务 completed 且重连对账结果与落盘全量一致、断连窗口补齐率 1.0；任务执行中重启：强杀后新进程续跑产出 `task_result` |
| 恢复耗时 | 断网：工具失败事件→下一次同工具成功事件的时间差；审批中重启：`approval_resumed`→`task_result` 时间差（resume 不补发 task_start，不能按 task_start 起算）；WS 断连：重连对账接口（GET /api/sessions/{thread_id}/events）调用耗时，毫秒级真实口径；任务执行中重启：恢复进程首事件（seq>死亡前末序号）→`task_result` 时间差 | 看 P95（最近邻秩，与 run_eval 口径一致） |

场景与口径约束：`disconnect`（fake Tavily 以 requests.ConnectionError 断网一次，验证 call_guard 重试恢复）；`restart_mid_approval`（standard 档停在 generate_markdown 审批中断 → 进程退出 = 进程死亡 → 新进程 `resume_deep_agent` 提交 approve 恢复，真实进程级重启语义）；`ws_disconnect`（进程内：mock-llm 任务全程跑完 = 服务端不受客户端断连影响、事件照常落盘；断连点取 task_start 后第 2 条，"重连"直调事件回放路由函数补齐断连窗口并核对全量一致——取证确认该函数签名仅 thread_id、读模块级 output_dir 常量，不依赖 FastAPI 请求上下文；真实 WS 层的握手鉴权/关闭码处理不在本场景覆盖内）；`process_restart_mid_task`（真实两段子进程：stage3 在 `assistant_call` 出现后短延时 `os._exit(1)` 强杀——死亡落在子智能体节点执行中而非审批中断点，checkpoint 停在最后一个已完成超步，击发点避开 sqlite 写事务窗口、半写由事务原子性兜底；stage4 新进程以 input=None 从 checkpoint 续跑，LangGraph 重放未完成超步，重复的检索调用被去重闸门拦截（blocked）不破坏收敛；恢复判定以事件流为准，收尾阶段无关异常只记录诊断不否定恢复）。**mock/real 双模式**：默认 mock-llm（ScriptedChatModel + 假 Tavily，零 token、CI 可跑）结果标注为机制验证；`--real-llm` 结果才计入简历数字。演练全程隔离：checkpoint 库与会话目录 patch 到演练 workdir，不碰真实数据。

