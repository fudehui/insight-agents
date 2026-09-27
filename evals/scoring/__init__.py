"""
evals.scoring：P0-1 评分骨架（W1）纯函数包

- citation_accuracy：引用准确率（报告引用 ↔ source_registry 登记来源比对）
- cost_latency：单 case 成本时延评分与 run 级聚合（最近邻秩 P95）

DeepEval（Faithfulness / AnswerRelevancy 等 LLM-as-Judge 指标）属 W3 接入，
不在本包范围。所有函数为纯函数、不调用任何 LLM/API，输入契约见各模块
docstring；指标口径对齐 docs/evaluation-metric-template.md 的 6 指标。
"""

from evals.scoring.citation_accuracy import extract_citations, score_citations
from evals.scoring.cost_latency import aggregate, nearest_rank_p95, score_case

__all__ = [
    "extract_citations",
    "score_citations",
    "score_case",
    "aggregate",
    "nearest_rank_p95",
]
