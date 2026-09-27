"""
评测跑批包（P0-1 W1 骨架）

- loader.py：golden case YAML 加载与 schema 校验
- run_eval.py：跑批 CLI，复用 run_deep_agent 执行用例并按固定契约落盘
- golden/：评测用例集（w1_batch.yaml 为 W1 的 10 条批次）
- results/：跑批输出目录（evals/results/{run_id}/case_{case_id}.json 与 summary.json）

指标口径衔接 docs/evaluation-metric-template.md，不在此重定义；详见 evals/README.md
"""
