"""
成本与时延评分模块（P0-1 评分三件套之三）

输入契约
--------
- score_case 输入：run_eval 产出的单 case JSON 字典
  （``evals/results/{run_id}/case_{case_id}.json``），本模块只读取其中
  ``token_usage``（{"input_tokens","output_tokens","total_tokens"} 或 null）
  与 ``duration_s`` 两个字段，其余字段忽略。
- aggregate 输入：per_case 统计条目列表，元素结构
  ``{"case_id", "status", "duration_s", "total_tokens"}``，与 run_eval 产出的
  summary.json 的 ``per_case`` 字段一致；status 取值 completed|failed|timeout，
  由 make_report 补充 missing/invalid 等标注态。

评分口径
--------
- 单任务 Token（模板指标 4）：取 token_usage.total_tokens；为 null 时
  total_tokens=None 并在 token_note 写明原因（缺埋点的 case 不参与 token
  汇总，与 2026-09-04 手工基线"旧会话缺埋点仅计入完成率"的取舍一致）；
  total_tokens 缺失但 input/output 齐全时按两者之和兜底并注明。
- 端到端耗时（模板指标 5）：duration_s（task_start → task_result 的秒数）。
- P95 用最近邻秩（nearest-rank）统计，与 run_eval 的 summary.json 同口径：
  样本升序排序后取第 r 名，r = ceil(0.95 * n)（1 基秩），对应 0 基下标 r-1；
  实现用整数运算 r = (95*n + 99) // 100 等价于 ceil(95n/100)，避免浮点误差。
  样本数 n < 20 时 r = n，P95 恰为最大值——8-19 条的小样本跑批（如手工基线
  的 8 条任务）P95 只反映最慢一条 case，n ≥ 20 后才是常规意义的 95 分位。
- aggregate 的 avg/P95 对传入样本的全部非 None duration 计算（含 failed/timeout
  的耗时：失败同样消耗真实时间）；total_tokens 对非 None 值求和，全部缺失时为
  None，部分缺失时为已知值之和（偏低估计，需结合 token 缺失标注解读）。
- 完成状态计数：status == "completed" 计入 cases_completed；
  status ∈ {"failed", "timeout"} 计入 cases_failed；其余值（含缺失、make_report
  的 missing/invalid 标注态）只计入 cases_run，既不计完成也不计失败——
  缺文件/坏文件的 case 不能默认算"完成"，避免完成率虚高。

与 docs/evaluation-metric-template.md 的对应
--------------------------------------------
- 指标 1 任务完成率、指标 4 单任务 Token、指标 5 端到端耗时由本模块
  （配合 evals/make_report.py）产出；
- 指标 2 数值准确率、指标 6 重复调用率不在本模块范围（W1 骨架不产出）。
"""

# 计入 cases_failed 的状态集合
_FAILED_STATUSES = ("failed", "timeout")


def _as_number(value) -> float | int | None:
    """把字段值安全转成数值；None/非数值（如空串）一律返回 None"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def score_case(case_json: dict) -> dict:
    """
    对单个 case 做成本时延评分

    :param case_json: run_eval 产出的单 case JSON 字典（见模块 docstring 契约）
    :return: {"total_tokens": int|None, "duration_s": float|None, "token_note": str}
        token_usage 为 null/空时 total_tokens 为 None，token_note 说明原因；
        duration_s 缺失或非数值时为 None（聚合计数时自动跳过）
    """
    data = case_json if isinstance(case_json, dict) else {}
    usage = data.get("token_usage")
    if isinstance(usage, dict) and usage:
        total = _as_number(usage.get("total_tokens"))
        if total is not None:
            token_note = ""
        else:
            input_tokens = _as_number(usage.get("input_tokens"))
            output_tokens = _as_number(usage.get("output_tokens"))
            if input_tokens is not None and output_tokens is not None:
                total = input_tokens + output_tokens
                token_note = "total_tokens 缺失，按 input_tokens + output_tokens 求和"
            else:
                token_note = "token_usage 存在但缺少可用的 token 字段"
    else:
        total = None
        token_note = "token_usage 为 null（该 case 无 token 埋点，不计入 token 汇总）"
    return {
        "total_tokens": None if total is None else int(total) if float(total).is_integer() else total,
        "duration_s": _as_number(data.get("duration_s")),
        "token_note": token_note,
    }


def nearest_rank_p95(samples) -> float | int | None:
    """
    最近邻秩 P95：升序第 ceil(0.95*n) 名（1 基秩 r，取 0 基下标 r-1）

    公式 r = ceil(0.95 * n)，实现为整数运算 (95*n + 99) // 100（等价于
    ceil(95n/100)，规避 0.95 的二进制浮点误差）。样本 n < 20 时 r = n，
    P95 即最大值（见模块 docstring）。空样本返回 None。
    """
    values = sorted(samples)
    n = len(values)
    if n == 0:
        return None
    rank = (95 * n + 99) // 100  # == ceil(0.95 * n)
    return values[rank - 1]


def aggregate(per_case_stats) -> dict:
    """
    聚合 per_case 统计条目为 run 级成本时延指标

    :param per_case_stats: 元素为 {"case_id","status","duration_s","total_tokens"}
        的列表（与 summary.json 的 per_case 同构；status 必须显式携带，
        缺失/未知值只计入 cases_run，见模块 docstring"完成状态计数"）
    :return: {"cases_run", "cases_completed", "cases_failed",
              "total_tokens"|None, "avg_duration_s"|None, "p95_duration_s"|None}
    """
    stats = list(per_case_stats or [])
    cases_run = len(stats)
    cases_completed = sum(1 for s in stats if s.get("status") == "completed")
    cases_failed = sum(1 for s in stats if s.get("status") in _FAILED_STATUSES)

    tokens = [n for s in stats if (n := _as_number(s.get("total_tokens"))) is not None]
    durations = [n for s in stats if (n := _as_number(s.get("duration_s"))) is not None]

    total_tokens = int(sum(tokens)) if tokens else None
    avg_duration = sum(durations) / len(durations) if durations else None
    return {
        "cases_run": cases_run,
        "cases_completed": cases_completed,
        "cases_failed": cases_failed,
        "total_tokens": total_tokens,
        "avg_duration_s": avg_duration,
        "p95_duration_s": nearest_rank_p95(durations),
    }
