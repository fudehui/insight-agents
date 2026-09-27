"""
评测报告生成 CLI（P0-1 W1 评分骨架）

输入契约
--------
读取 run_eval.py 产出的 run 目录：
- ``evals/results/{run_id}/case_{case_id}.json``：单 case 文件（字段契约见
  evals/scoring 各模块 docstring）；
- ``evals/results/{run_id}/summary.json``：可选。存在且可解析时，以其中
  ``per_case[].case_id`` 清单为 case 全集（可发现"文件缺失"的 case），
  run_id 优先取 summary 的 ``run_id``；缺失或解析失败时以 glob 到的
  ``case_*.json`` 为全集，run_id 取目录名。

用法
----
    python -m evals.make_report --run-dir evals/results/run_a
    python -m evals.make_report --run-dir evals/results/run_a \
        --run-dir evals/results/run_b --out evals/report.md

--run-dir 可传 1-2 个；传两个时输出 X→Y 对比表，第一个 run 为基线 X、
第二个为 Y，差值 = Y − X。

评分口径
--------
- 成本时延汇总复用 evals/scoring/cost_latency.aggregate（P95 为最近邻秩，
  样本 <20 时即最大值，口径说明见该模块 docstring）；
- 每 case 引用评分复用 citation_accuracy.score_citations，仅对
  status=completed 的 case 执行；文件缺失 / JSON 解析失败 / status != completed
  的 case 只在明细表标注并跳过评分；
- 整体引用准确率 = 各完成 case 的 matched 之和 ÷ unique_cited 之和（微平均，
  引用越多的 case 权重越大）；无任何引用时写 "—"；
- 引用覆盖率（模板指标 3）= matched 之和 ÷ 完成 case 登记来源数之和（微平均）；
- 对比表固定使用 docs/evaluation-metric-template.md 的 6 指标名；指标 2
  （数值准确率）与指标 6（重复调用率）W1 骨架不产出，写 "—" 占位；
  比率类差值单位为百分点（pp），缺失指标写 "—"。
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    from evals.scoring.citation_accuracy import score_citations
    from evals.scoring.cost_latency import aggregate, score_case
except ImportError:  # 直接以 `python evals/make_report.py` 运行时，补项目根到 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from evals.scoring.citation_accuracy import score_citations
    from evals.scoring.cost_latency import aggregate, score_case

_DEFAULT_OUT = "evals/report.md"

# docs/evaluation-metric-template.md 定义的 6 指标名（对比表行序与其一致）
_TEMPLATE_METRICS = (
    "任务完成率",
    "数值准确率（DB 任务）",
    "引用覆盖率",
    "单任务 Token",
    "端到端耗时",
    "重复调用率",
)

_CASE_FILE_PREFIX = "case_"


def _load_case_row(run_dir: Path, case_id: str) -> dict:
    """
    读取单个 case 文件并产出明细行

    行结构：{"case_id","status","duration_s","total_tokens","token_note",
             "citation","sources_count","note"}。
    文件缺失 → status="missing"；JSON 解析失败 → status="invalid"；
    两者均跳过评分（citation=None）。status != completed 的 case 同样跳过
    引用评分，error 截断到 80 字符进备注。
    """
    row = {
        "case_id": case_id,
        "status": "missing",
        "duration_s": None,
        "total_tokens": None,
        "token_note": "",
        "citation": None,
        "sources_count": 0,
        "note": "",
    }
    case_path = run_dir / f"{_CASE_FILE_PREFIX}{case_id}.json"
    if not case_path.exists():
        row["note"] = f"文件缺失：{case_path.name} 不存在"
        return row
    try:
        data = json.loads(case_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        row["status"] = "invalid"
        row["note"] = f"JSON 解析失败，已跳过：{exc}"[:120]
        return row
    if not isinstance(data, dict):
        row["status"] = "invalid"
        row["note"] = "case JSON 不是对象，已跳过"
        return row

    status = str(data.get("status") or "").strip()
    stats = score_case(data)
    row["status"] = status or "unknown"
    row["duration_s"] = stats["duration_s"]
    row["total_tokens"] = stats["total_tokens"]
    row["token_note"] = stats["token_note"]
    row["sources_count"] = len(data.get("sources") or [])
    if status == "completed":
        try:
            row["citation"] = score_citations(data.get("report_md"), data.get("sources") or [])
        except ValueError as exc:
            row["note"] = f"引用评分跳过：{exc}"[:120]
    else:
        note = f"未评分：status={row['status']}"
        error = str(data.get("error") or "").strip()
        if error:
            note += f"；error={error[:80]}"
        row["note"] = note
    return row


def load_run(run_dir: Path) -> dict:
    """
    读取一个 run 目录，返回 {"run_id", "rows", "has_summary"}

    case 全集：summary.json 的 per_case 顺序优先，glob 到的多余 case 文件
    追加在后（两者都按首次出现去重）。
    """
    run_dir = Path(run_dir)
    summary = None
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[make_report] {summary_path} 解析失败，按无 summary 处理：{exc}")

    ordered_ids: list[str] = []
    if isinstance(summary, dict):
        for item in summary.get("per_case") or []:
            case_id = str(item.get("case_id") or "").strip()
            if case_id and case_id not in ordered_ids:
                ordered_ids.append(case_id)

    file_ids: list[str] = []
    for case_path in sorted(run_dir.glob(f"{_CASE_FILE_PREFIX}*.json")):
        case_id = case_path.stem[len(_CASE_FILE_PREFIX) :]
        if case_id and case_id not in file_ids:
            file_ids.append(case_id)

    all_ids = ordered_ids + [cid for cid in file_ids if cid not in ordered_ids]
    rows = [_load_case_row(run_dir, case_id) for case_id in all_ids]
    run_id = str(summary.get("run_id") or "").strip() if isinstance(summary, dict) else ""
    # DeepEval 结果（deepeval_runner.score_run 产物）：存在时给明细行与汇总
    # 附带 LLM-as-Judge 分数；文件缺失时保持原行为（全 "—"，不出列）
    deepeval_by_case, deepeval_meta = _load_deepeval(run_dir)
    for row in rows:
        scores = deepeval_by_case.get(row["case_id"])
        row["deepeval"] = scores
    return {
        "run_id": run_id or run_dir.name,
        "rows": rows,
        "has_summary": summary is not None,
        "deepeval_meta": deepeval_meta,
    }


def _load_deepeval(run_dir: Path) -> tuple[dict[str, dict], dict | None]:
    """
    读取 run 目录的 deepeval.json（DeepEval 评分，deepeval_runner.py 产出）

    :return: (case_id -> {faithfulness, answer_relevancy} 映射, 元信息 dict 或 None)
    """
    path = run_dir / "deepeval.json"
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[make_report] {path} 解析失败，按无 DeepEval 结果处理：{exc}")
        return {}, None
    if not isinstance(payload, dict):
        return {}, None
    by_case = {
        str(item.get("case_id")): {
            "faithfulness": item.get("faithfulness"),
            "answer_relevancy": item.get("answer_relevancy"),
        }
        for item in payload.get("cases") or []
        if isinstance(item, dict) and item.get("case_id")
    }
    meta = {
        "judge_model": payload.get("judge_model") or "—",
        "threshold": payload.get("threshold"),
    }
    return by_case, meta


def _deepeval_avg(by_case_scores: list[dict], key: str) -> float | None:
    """对有分的 case 取均值（None 分数不参与）；无任何分数返回 None"""
    values = [s[key] for s in by_case_scores if s and s.get(key) is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def run_metrics(run: dict) -> dict:
    """
    汇总单个 run 的指标（在 load_run 结果上计算）

    返回 dict 包含 run_id / rows / has_summary / agg（cost_latency.aggregate
    结果）/ accuracy（(matched, unique) 或 None）/ coverage（(matched, registered)
    或 None）/ token_missing（是否有 case 缺 token 埋点）。
    """
    rows = run["rows"]
    stats = [
        {
            "case_id": row["case_id"],
            "status": row["status"],
            "duration_s": row["duration_s"],
            "total_tokens": row["total_tokens"],
        }
        for row in rows
    ]
    agg = aggregate(stats)
    cited_rows = [row for row in rows if row.get("citation")]
    matched = sum(row["citation"]["matched"] for row in cited_rows)
    unique_cited = sum(row["citation"]["unique_cited"] for row in cited_rows)
    registered = sum(row["sources_count"] for row in cited_rows)
    deepeval_scores = [row.get("deepeval") or {} for row in rows]
    return {
        "run_id": run["run_id"],
        "rows": rows,
        "has_summary": run["has_summary"],
        "agg": agg,
        "accuracy": (matched, unique_cited) if unique_cited else None,
        "coverage": (matched, registered) if registered else None,
        "token_missing": any(row["total_tokens"] is None for row in rows),
        "deepeval_meta": run.get("deepeval_meta"),
        "deepeval": {
            "faithfulness": _deepeval_avg(deepeval_scores, "faithfulness"),
            "answer_relevancy": _deepeval_avg(deepeval_scores, "answer_relevancy"),
        },
    }


def _fmt_ratio(pair) -> str:
    """(分子, 分母) → "50.0%（2/4）"；分母为 0 或 None 时写 "—\""""
    if not pair or not pair[1]:
        return "—"
    numerator, denominator = pair
    return f"{100.0 * numerator / denominator:.1f}%（{numerator}/{denominator}）"


def _fmt_duration(value) -> str:
    """秒数 → "12.3"；缺失写 "—\""""
    return "—" if value is None else f"{value:.1f}"


def _fmt_signed(value, unit: str = "") -> str:
    """带符号差值："+1.5pp" / "-123"；缺失写 "—\""""
    if value is None:
        return "—"
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):.1f}{unit}" if isinstance(value, float) else f"{sign}{abs(value)}{unit}"


def _ratio_diff(x_pair, y_pair) -> float | None:
    """比率差（百分点）；任一侧分母为 0/None 时返回 None"""
    if not x_pair or not x_pair[1] or not y_pair or not y_pair[1]:
        return None
    x_rate = x_pair[0] / x_pair[1]
    y_rate = y_pair[0] / y_pair[1]
    return (y_rate - x_rate) * 100.0


def _fmt_score(value) -> str:
    """DeepEval 分数（0-1）→ "0.7500"；None → "—\""""
    return "—" if value is None else f"{value:.4f}"


def _detail_table(rows: list[dict], with_deepeval: bool = False) -> list[str]:
    """每 case 一行的明细表（缺文件/失败/缺埋点均在备注列标注）；有 DeepEval
    结果时追加忠实度/相关性两列"""
    header = "| case_id | status | 耗时(s) | token | 引用准确率 |"
    sep = "| --- | --- | --- | --- | --- |"
    if with_deepeval:
        header += " 忠实度 | 相关性 |"
        sep += " --- | --- |"
    header += " 备注 |"
    sep += " --- |"
    lines = [header, sep]
    for row in rows:
        citation = row.get("citation")
        if citation is not None:
            accuracy_cell = _fmt_ratio((citation["matched"], citation["unique_cited"]))
        else:
            accuracy_cell = "—"
        if with_deepeval:
            scores = row.get("deepeval") or {}
            faith_cell = _fmt_score(scores.get("faithfulness"))
            relevancy_cell = _fmt_score(scores.get("answer_relevancy"))
        else:
            faith_cell = relevancy_cell = ""
        notes = [item for item in (row.get("note"), row.get("token_note")) if item]
        line = (
            "| {case_id} | {status} | {duration} | {tokens} | {accuracy} |{extra} {note} |"
        ).format(
            case_id=row["case_id"],
            status=row["status"],
            duration=_fmt_duration(row["duration_s"]),
            tokens="—" if row["total_tokens"] is None else row["total_tokens"],
            accuracy=accuracy_cell,
            extra=(
                f" {faith_cell} | {relevancy_cell} |"
                if with_deepeval
                else ""
            ),
            note="；".join(notes) or " ",
        )
        lines.append(line)
    return lines


def _summary_block(metrics: dict) -> list[str]:
    """单个 run 的顶部汇总表（cases / 完成率 / 总 token / 平均与 P95 时延 /
    整体引用准确率 / 引用覆盖率）"""
    agg = metrics["agg"]
    lines = [
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| cases | {agg['cases_run']}（完成 {agg['cases_completed']} / 失败 {agg['cases_failed']}） |",
        f"| 任务完成率 | {_fmt_ratio((agg['cases_completed'], agg['cases_run']))} |",
        f"| 单任务 Token（合计） | {'—' if agg['total_tokens'] is None else agg['total_tokens']} |",
        f"| 端到端耗时（平均） | {_fmt_duration(agg['avg_duration_s'])} s |",
        f"| 端到端耗时（P95，最近邻秩） | {_fmt_duration(agg['p95_duration_s'])} s |",
        f"| 整体引用准确率 | {_fmt_ratio(metrics['accuracy'])} |",
        f"| 引用覆盖率 | {_fmt_ratio(metrics['coverage'])} |",
    ]
    if metrics.get("deepeval_meta"):
        meta = metrics["deepeval_meta"]
        d = metrics["deepeval"]
        lines.append(f"| 忠实度（Faithfulness，judge={meta['judge_model']}） | {_fmt_score(d['faithfulness'])} |")
        lines.append(
            f"| 答案相关性（AnswerRelevancy，阈值 {meta.get('threshold')}） | {_fmt_score(d['answer_relevancy'])} |"
        )
    lines.append("")
    if metrics["token_missing"]:
        lines.append(
            "> 注：部分 case 的 token_usage 为 null，token 合计为已知值之和（偏低估计）。"
        )
        lines.append("")
    return lines


def _comparison_rows(x: dict, y: dict) -> list[str]:
    """两个 run 的 6 指标并排对比表（X→Y 口径，差值 = Y − X）"""
    x_agg, y_agg = x["agg"], y["agg"]

    completion_diff = _ratio_diff(
        (x_agg["cases_completed"], x_agg["cases_run"]),
        (y_agg["cases_completed"], y_agg["cases_run"]),
    )
    coverage_diff = _ratio_diff(x["coverage"], y["coverage"])
    accuracy_diff = _ratio_diff(x["accuracy"], y["accuracy"])

    if x_agg["total_tokens"] is None or y_agg["total_tokens"] is None:
        token_diff = None
    else:
        token_diff = y_agg["total_tokens"] - x_agg["total_tokens"]

    if None in (x_agg["avg_duration_s"], y_agg["avg_duration_s"], x_agg["p95_duration_s"], y_agg["p95_duration_s"]):
        duration_diff = "—"
    else:
        duration_diff = " / ".join(
            [
                f"{y_agg['avg_duration_s'] - x_agg['avg_duration_s']:+.1f}",
                f"{y_agg['p95_duration_s'] - x_agg['p95_duration_s']:+.1f}",
            ]
        )

    duration_x = f"{_fmt_duration(x_agg['avg_duration_s'])} / {_fmt_duration(x_agg['p95_duration_s'])}"
    duration_y = f"{_fmt_duration(y_agg['avg_duration_s'])} / {_fmt_duration(y_agg['p95_duration_s'])}"

    rows = [
        ("任务完成率", _fmt_ratio((x_agg["cases_completed"], x_agg["cases_run"])),
         _fmt_ratio((y_agg["cases_completed"], y_agg["cases_run"])),
         "—" if completion_diff is None else _fmt_signed(completion_diff, "pp")),
        (f"{_TEMPLATE_METRICS[1]}", "—", "—", "—"),
        (_TEMPLATE_METRICS[2], _fmt_ratio(x["coverage"]), _fmt_ratio(y["coverage"]),
         "—" if coverage_diff is None else _fmt_signed(coverage_diff, "pp")),
        (_TEMPLATE_METRICS[3],
         "—" if x_agg["total_tokens"] is None else x_agg["total_tokens"],
         "—" if y_agg["total_tokens"] is None else y_agg["total_tokens"],
         _fmt_signed(token_diff)),
        (_TEMPLATE_METRICS[4], duration_x, duration_y, duration_diff),
        (f"{_TEMPLATE_METRICS[5]}", "—", "—", "—"),
        ("整体引用准确率（补充指标）", _fmt_ratio(x["accuracy"]), _fmt_ratio(y["accuracy"]),
         "—" if accuracy_diff is None else _fmt_signed(accuracy_diff, "pp")),
    ]
    # DeepEval 行：任一 run 有评分结果才输出（无结果的 run 显示 "—"）
    if x.get("deepeval_meta") or y.get("deepeval_meta"):
        for key, label in (
            ("faithfulness", "忠实度（DeepEval Faithfulness，0-1）"),
            ("answer_relevancy", "答案相关性（DeepEval AnswerRelevancy，0-1）"),
        ):
            x_v, y_v = x["deepeval"][key], y["deepeval"][key]
            diff = (
                "—"
                if x_v is None or y_v is None
                else _fmt_signed(round(y_v - x_v, 4))
            )
            rows.append((label, _fmt_score(x_v), _fmt_score(y_v), diff))
    lines = [
        "| 指标（docs/evaluation-metric-template.md） | {} | {} | 差值（Y−X） |".format(x["run_id"], y["run_id"]),
        "| --- | --- | --- | --- |",
    ]
    lines.extend(f"| {name} | {x_cell} | {y_cell} | {diff_cell} |" for name, x_cell, y_cell, diff_cell in rows)
    return lines


def build_report(metrics_list: list[dict]) -> str:
    """
    把 1-2 个 run 的指标渲染成 markdown 报告

    单 run：汇总表 + 每 case 明细表；双 run：6 指标对比表（第一个 run 为
    基线 X）+ 各 run 明细表。
    """
    if not 1 <= len(metrics_list) <= 2:
        raise ValueError(f"build_report 仅支持 1-2 个 run，收到 {len(metrics_list)} 个")
    generated_at = datetime.now().isoformat(timespec="seconds")
    lines: list[str] = []
    if len(metrics_list) == 1:
        metrics = metrics_list[0]
        lines.append(f"# 评测报告：{metrics['run_id']}")
        lines.append("")
        lines.append(f"- 生成时间：{generated_at}")
        lines.append(
            "- 口径：P95 为最近邻秩（样本 <20 时即最大值）；指标名对齐 "
            "docs/evaluation-metric-template.md；评分细节见 evals/scoring 模块 docstring"
        )
        lines.append("")
        lines.extend(_summary_block(metrics))
        lines.append("## 每 case 明细")
        lines.append("")
        lines.extend(_detail_table(metrics["rows"], with_deepeval=bool(metrics.get("deepeval_meta"))))
        lines.append("")
    else:
        x, y = metrics_list
        lines.append(f"# 评测对比：{x['run_id']} → {y['run_id']}")
        lines.append("")
        lines.append(f"- 生成时间：{generated_at}")
        lines.append(
            "- X→Y 口径：第一个 run 为基线 X，第二个为 Y，差值 = Y − X；"
            "比率类差值单位为百分点（pp），缺失指标写 —"
        )
        lines.append(
            "- 指标名对齐 docs/evaluation-metric-template.md 的 6 指标；数值准确率"
            "（DB 任务）与重复调用率 W1 骨架不产出，以 — 占位"
        )
        lines.append("")
        lines.extend(_comparison_rows(x, y))
        lines.append("")
        for metrics in metrics_list:
            lines.append(f"## {metrics['run_id']} 每 case 明细")
            lines.append("")
            lines.extend(_detail_table(metrics["rows"], with_deepeval=bool(metrics.get("deepeval_meta"))))
            lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析 --run-dir（1-2 个）与 --out，生成并写出报告"""
    parser = argparse.ArgumentParser(
        prog="python -m evals.make_report",
        description="根据 run_eval 产出的 run 目录生成 markdown 评测报告（P0-1 W1）",
    )
    parser.add_argument(
        "--run-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="run 结果目录（evals/results/{run_id}），可传 1-2 个；两个时输出 X→Y 对比",
    )
    parser.add_argument("--out", default=_DEFAULT_OUT, help=f"输出 markdown 路径（默认 {_DEFAULT_OUT}）")
    args = parser.parse_args(argv)

    if not 1 <= len(args.run_dir) <= 2:
        parser.error("--run-dir 需要提供 1-2 个（可重复传参）")
    run_dirs = [Path(path) for path in args.run_dir]
    for run_dir in run_dirs:
        if not run_dir.is_dir():
            parser.error(f"run 目录不存在或不是目录：{run_dir}")

    metrics_list = [run_metrics(load_run(run_dir)) for run_dir in run_dirs]
    for metrics in metrics_list:
        agg = metrics["agg"]
        print(
            f"[make_report] {metrics['run_id']}: "
            f"cases={agg['cases_run']} 完成={agg['cases_completed']} 失败={agg['cases_failed']} "
            f"引用准确率={_fmt_ratio(metrics['accuracy'])}"
        )

    report_text = build_report(metrics_list)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report_text, encoding="utf-8")
    print(f"[make_report] 报告已写入 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
