"""
DeepEval 评分模块的单元测试（W3）

全程不调用真实 judge：monkeypatch deepeval_runner.score_case 内部依赖或
直接注入假 judge；覆盖评分、跳过口径、异常捕获、增量合并与 make_report 的
有/无 deepeval.json 两种渲染。
"""

import json
from pathlib import Path

import pytest

from evals import deepeval_runner, make_report


def _write_case(results_dir: Path, case_id: str, status="completed", report="# 报告\n正文"):
    record = {
        "case_id": case_id,
        "query": "测试问题",
        "session_id": f"eval_x_{case_id}",
        "report_md": report if status == "completed" else None,
        "report_path": None,
        "sources": [{"type": "web", "title": "T", "url_or_ref": "https://e.com"}],
        "token_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        "duration_s": 1.0,
        "status": status,
        "error": None if status == "completed" else "boom",
        "started_at": "t0",
        "finished_at": "t1",
    }
    (results_dir / f"case_{case_id}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    return record


class _FakeJudge:
    """满足 deepeval_runner.score_case 注入要求的假 judge（不可调用版）"""


def test_score_case_completed_with_fake_metrics(tmp_path, monkeypatch):
    _write_case(tmp_path, "c1")

    # monkeypatch 指标类：Faithfulness 返回 0.8，AnswerRelevancy 返回 0.6
    class FakeMetric:
        def __init__(self, threshold=None, model=None, include_reason=None):
            self.threshold = threshold

        def measure(self, test_case):
            return 0.8 if isinstance(self.threshold, float) and self.threshold >= 0.5 and False else 0.8

    scores = {}
    real_init = None
    import deepeval.metrics as m

    class FakeFaith(m.FaithfulnessMetric):
        def __init__(self, threshold=None, model=None, include_reason=None):
            self.threshold = threshold  # 覆盖原 __init__：不校验 model 类型、不触网

        def measure(self, test_case):
            scores["faith"] = 0.8
            return 0.8

    class FakeRelev(m.AnswerRelevancyMetric):
        def __init__(self, threshold=None, model=None, include_reason=None):
            self.threshold = threshold

        def measure(self, test_case):
            scores["rel"] = 0.6
            return 0.6

    monkeypatch.setattr(m, "FaithfulnessMetric", FakeFaith, raising=False)
    monkeypatch.setattr(m, "AnswerRelevancyMetric", FakeRelev, raising=False)
    monkeypatch.setattr(deepeval_runner, "_build_judge", lambda: (_FakeJudge(), "fake-model"))
    # LLMTestCase 正常构造即可（输入均为字符串，不触网）
    result = deepeval_runner.score_case(
        json.loads((tmp_path / "case_c1.json").read_text(encoding="utf-8")),
    )

    assert result["faithfulness"] == 0.8
    assert result["answer_relevancy"] == 0.6
    assert result["error"] is None
    assert result["judge_model"] == "fake-model"


def test_score_case_skips_failed_and_empty_report(tmp_path):
    failed = _write_case(tmp_path, "c2", status="failed")
    result = deepeval_runner.score_case(failed, judge=_FakeJudge())
    assert result["faithfulness"] is None
    assert "跳过" in (result["error"] or "")

    empty = _write_case(tmp_path, "c3", report="   ")
    result = deepeval_runner.score_case(empty, judge=_FakeJudge())
    assert result["faithfulness"] is None
    assert "report_md 为空" in (result["error"] or "")


def test_score_case_captures_judge_exception(tmp_path, monkeypatch):
    record = _write_case(tmp_path, "c4")

    import deepeval.metrics as m

    class BoomMetric:
        def __init__(self, **kwargs):
            pass

        def measure(self, test_case):
            raise RuntimeError("429 rate limit")

    monkeypatch.setattr(m, "FaithfulnessMetric", BoomMetric, raising=False)
    monkeypatch.setattr(m, "AnswerRelevancyMetric", BoomMetric, raising=False)

    result = deepeval_runner.score_case(record, judge=_FakeJudge())
    assert result["faithfulness"] is None
    assert result["answer_relevancy"] is None
    assert "429" in result["error"]


def test_score_run_incremental_merges(tmp_path, monkeypatch):
    """已有 deepeval.json 中成功出分的 case 跳过，只重跑缺失/出错的"""
    _write_case(tmp_path, "c1")
    _write_case(tmp_path, "c2")

    # 预置：c1 已有分数、c2 上次出错
    (tmp_path / "deepeval.json").write_text(
        json.dumps(
            {
                "judge_model": "old-model",
                "threshold": 0.5,
                "cases": [
                    {"case_id": "c1", "faithfulness": 0.9, "answer_relevancy": 0.9, "error": None},
                    {"case_id": "c2", "faithfulness": None, "answer_relevancy": None, "error": "429"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    calls = []

    def fake_score_case(record, **kwargs):
        calls.append(record["case_id"])
        return {
            "case_id": record["case_id"],
            "faithfulness": 0.5,
            "answer_relevancy": 0.5,
            "judge_model": kwargs.get("model_name") or "new-model",
            "error": None,
        }

    monkeypatch.setattr(deepeval_runner, "_build_judge", lambda: (_FakeJudge(), "new-model"))
    monkeypatch.setattr(deepeval_runner, "score_case", fake_score_case)

    out = deepeval_runner.score_run(tmp_path)

    assert calls == ["c2"]  # 只有上次出错的 c2 被重跑
    payload = json.loads(out.read_text(encoding="utf-8"))
    by_id = {c["case_id"]: c for c in payload["cases"]}
    assert by_id["c1"]["faithfulness"] == 0.9  # 保留旧分
    assert by_id["c2"]["faithfulness"] == 0.5  # 新分覆盖
    assert payload["judge_model"] == "new-model"


def test_make_report_with_and_without_deepeval(tmp_path):
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _write_case(run_dir, "c1")
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "run1",
                "per_case": [{"case_id": "c1", "status": "completed"}],
                "cases_run": 1,
                "cases_completed": 1,
                "cases_failed": 0,
                "total_tokens": 2,
                "p95_duration_s": 1.0,
            }
        ),
        encoding="utf-8",
    )

    # 无 deepeval.json：不出 DeepEval 列（与旧行为一致）
    metrics = make_report.run_metrics(make_report.load_run(run_dir))
    report = make_report.build_report([metrics])
    assert "忠实度" not in report
    assert "| case_id | status | 耗时(s) | token | 引用准确率 | 备注 |" in report

    # 有 deepeval.json：汇总出均值行、明细表追加两列
    (run_dir / "deepeval.json").write_text(
        json.dumps(
            {
                "judge_model": "judge-x",
                "threshold": 0.5,
                "cases": [
                    {"case_id": "c1", "faithfulness": 0.8, "answer_relevancy": 0.6, "error": None}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    metrics = make_report.run_metrics(make_report.load_run(run_dir))
    report = make_report.build_report([metrics])
    assert "忠实度（Faithfulness，judge=judge-x） | 0.8000" in report
    assert "答案相关性" in report and "0.6000" in report
    assert "| case_id | status | 耗时(s) | token | 引用准确率 | 忠实度 | 相关性 | 备注 |" in report
    assert "| c1 | completed" in report and "0.8000 | 0.6000 |" in report
