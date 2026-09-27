"""
DeepEval LLM-as-Judge 评分模块（P0-1 W3）

对 run_eval 产出的 case JSON 跑 Faithfulness（忠实度：报告是否忠于登记来源）
与 AnswerRelevancy（答案相关性：报告是否切题）两个指标，结果落盘
evals/results/{run_id}/deepeval.json，由 evals/make_report.py 聚合进评测报告。

指标映射（docs/evaluation-metric-template.md 的"DeepEval 指标映射"小节）：
- FaithfulnessMetric：input=query、actual_output=report_md、
  retrieval_context=sources 的 title/url_or_ref 拼接——衡量"报告结论是否有
  来源支撑"，是对人工基线中"引用覆盖率"的质量维度补充；
- AnswerRelevancyMetric：input=query、actual_output=report_md——衡量"报告
  是否回答了任务问的问题"。

成本控制（升级计划 P0-1：judge 用低价模型控成本）：
- judge 模型取环境变量 EVAL_JUDGE_MODEL，未设置时回退 LLM_QWEN_MAX（同一
  OpenAI 兼容网关）；judge 调用走同一 OPENAI_API_KEY / OPENAI_BASE_URL；
- score_run 支持 cases 过滤，先小批量冒烟再全量；单 case 评分异常捕获为
  error 字段，不炸整批（免费档 429 窗口常见）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_THRESHOLD = 0.5


def _judge_model_name() -> str:
    """judge 模型名：EVAL_JUDGE_MODEL 优先，回退业务主模型同名档"""
    return os.getenv("EVAL_JUDGE_MODEL") or os.getenv("LLM_QWEN_MAX") or ""


class _OpenAICompatJudge:
    """把 OpenAI 兼容网关包装成 DeepEval 的自定义模型基类"""

    def __init__(self, model_name: str):
        from openai import AsyncOpenAI, OpenAI
        from deepeval.models import DeepEvalBaseLLM

        base = DeepEvalBaseLLM

        outer = self

        class _Model(base):  # noqa: N801  DeepEval 约定继承其基类
            def __init__(self):
                api_key = os.getenv("OPENAI_API_KEY")
                base_url = os.getenv("OPENAI_BASE_URL")
                self.sync_client = OpenAI(api_key=api_key, base_url=base_url)
                self.async_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
                super().__init__(model_name=model_name)

            def load_model(self):
                return self.sync_client

            def get_model_name(self) -> str:
                return model_name

            def generate(self, prompt: str) -> str:
                resp = self.sync_client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                )
                return resp.choices[0].message.content or ""

            async def a_generate(self, prompt: str) -> str:
                resp = await self.async_client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                )
                return resp.choices[0].message.content or ""

        outer._model_cls = _Model
        outer.DeepEvalBaseLLM = base

    def build(self):
        return self._model_cls()


def _build_judge():
    """构造 judge 模型实例；DeepEval 缺失时抛出可读错误"""
    model_name = _judge_model_name()
    if not model_name:
        raise RuntimeError("EVAL_JUDGE_MODEL / LLM_QWEN_MAX 均未设置，无法构造 judge")
    wrapper = _OpenAICompatJudge(model_name)
    return wrapper.build(), model_name


def _retrieval_context(sources: list[dict[str, str]]) -> list[str]:
    """来源清单转 retrieval_context：title + url_or_ref 拼接（忠实度比对依据）"""
    return [
        f"{s.get('title') or ''} {s.get('url_or_ref') or ''}".strip()
        for s in (sources or [])
        if isinstance(s, dict)
    ]


def score_case(
    record: dict[str, Any],
    threshold: float = DEFAULT_THRESHOLD,
    judge=None,
    model_name: Optional[str] = None,
) -> dict[str, Any]:
    """
    对单条 case 记录跑两个指标

    status != completed 或报告为空 → 两项 None（与 make_report 的"—"口径一致）；
    judge 调用异常 → error 字段记录原因，分数置 None。
    """
    result: dict[str, Any] = {
        "case_id": record.get("case_id"),
        "faithfulness": None,
        "answer_relevancy": None,
        "judge_model": model_name or _judge_model_name(),
        "error": None,
    }
    if record.get("status") != "completed":
        result["error"] = f"跳过：status={record.get('status')}"
        return result
    report_md = record.get("report_md") or ""
    if not report_md.strip():
        result["error"] = "跳过：report_md 为空"
        return result

    if judge is None:
        judge, model_name = _build_judge()
        result["judge_model"] = model_name

    from deepeval.test_case import LLMTestCase

    test_case = LLMTestCase(
        input=record.get("query") or "",
        actual_output=report_md,
        retrieval_context=_retrieval_context(record.get("sources") or []),
    )

    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric

    for name, metric in (
        ("faithfulness", FaithfulnessMetric(threshold=threshold, model=judge, include_reason=False)),
        ("answer_relevancy", AnswerRelevancyMetric(threshold=threshold, model=judge, include_reason=False)),
    ):
        try:
            start = time.perf_counter()
            score = float(metric.measure(test_case))
            result[name] = round(score, 4)
            print(
                f"[deepeval] {result['case_id']} {name}={result[name]} "
                f"({time.perf_counter() - start:.0f}s)"
            )
        except Exception as e:  # judge API 429/断流等：单 case 失败不炸整批
            result[name] = None
            result["error"] = f"{type(e).__name__}: {str(e)[:150]}"
            print(f"[deepeval] {result['case_id']} {name} 失败：{result['error']}")
    return result


def score_run(
    results_dir: Path,
    case_ids: Optional[list[str]] = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> Path:
    """
    对 run 目录内全部（或指定）case 跑 DeepEval，结果写 deepeval.json

    已有 deepeval.json 时增量合并：仅对缺失/出错（error 非跳过类）的 case 重跑，
    便于 429 冷却后补跑而不重复烧 judge token
    """
    deepeval_file = results_dir / "deepeval.json"
    existing: dict[str, Any] = {}
    if deepeval_file.is_file():
        try:
            existing = json.loads(deepeval_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    by_case = {c.get("case_id"): c for c in existing.get("cases", [])}

    judge, model_name = _build_judge()
    for case_file in sorted(results_dir.glob("case_*.json")):
        record = json.loads(case_file.read_text(encoding="utf-8"))
        cid = record.get("case_id") or case_file.stem.removeprefix("case_")
        if case_ids and cid not in case_ids:
            continue
        prior = by_case.get(cid) or {}
        # 增量：上次已成功出分的跳过；出错或缺失的重跑
        if prior.get("faithfulness") is not None and prior.get("answer_relevancy") is not None:
            continue
        print(f"[deepeval] 评分 {cid} ...")
        by_case[cid] = score_case(record, threshold=threshold, judge=judge, model_name=model_name)

    payload = {
        "judge_model": model_name,
        "threshold": threshold,
        "cases": [by_case[k] for k in sorted(by_case)],
    }
    deepeval_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[deepeval] 结果已写入 {deepeval_file}")
    return deepeval_file


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="deepeval_runner",
        description="对 run_eval 结果跑 DeepEval（Faithfulness / AnswerRelevancy）",
    )
    parser.add_argument("--run-dir", required=True, help="run 结果目录 evals/results/{run_id}")
    parser.add_argument("--case", action="append", default=None, help="只评分指定 case id，可重复")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD, help="指标阈值（默认 0.5）"
    )
    args = parser.parse_args(argv)
    score_run(Path(args.run_dir), case_ids=args.case, threshold=args.threshold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
