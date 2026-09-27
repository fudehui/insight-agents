"""
evals 评分骨架（P0-1 W1）的单元测试

覆盖：引用抽取与匹配（fixture 按真实报告格式构造，见
evals/scoring/citation_accuracy.py 的格式取证）、accuracy 计算、成本时延口径
（最近邻秩 P95、token 缺埋点）、make_report 单 run 汇总与双 run 对比的关键行、
缺文件 / 坏 JSON / 空目录容错
"""

import json
from pathlib import Path

import pytest

from evals.make_report import load_run, main
from evals.scoring.citation_accuracy import extract_citations, score_citations
from evals.scoring.cost_latency import aggregate, nearest_rank_p95, score_case

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "evals"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _write_case(run_dir: Path, case_id: str, case_json: dict) -> None:
    """按 run_eval 的命名契约 case_{case_id}.json 写入伪造 case 文件"""
    (run_dir / f"case_{case_id}.json").write_text(
        json.dumps(case_json, ensure_ascii=False), encoding="utf-8"
    )


class TestExtractCitations:
    def test_primary_bracket_format(self):
        # 真实报告的主格式：正文中 [1]、[2][4] 等编号标记
        assert extract_citations("成本下降 [1]；专利占比 [2][4]，结论 [12]") == [
            "1",
            "2",
            "4",
            "12",
        ]

    def test_duplicates_preserved_in_order(self):
        assert extract_citations("[1] 与 [1][3]") == ["1", "1", "3"]

    def test_footnote_and_fullwidth_variants(self):
        assert extract_citations("脚注式[^1]，全角式【2】") == ["1", "2"]

    def test_ignores_years_link_defs_and_reference_section(self):
        report = (
            "正文引用 [1]。\n"
            "[1]: https://example.com/def\n"  # 链接定义行不是引用
            "[2026] 年鉴不算引用。\n"  # 方括号年份（4 位数字）不匹配
            "## 【参考来源】\n"
            "1. [2024、2025年报告](https://example.com/a)\n"  # 参考章节不抽正文标记
            "2. [2025](https://example.com/b)\n"
        )
        assert extract_citations(report) == ["1"]

    def test_empty_report(self):
        assert extract_citations("") == []
        assert extract_citations(None) == []


class TestScoreCitations:
    def test_full_match_fixture(self):
        # fixture 按真实报告格式构造：统一编号参考章节 + 正文 [N] 标记
        case = _load_fixture("case_cited_full.json")
        result = score_citations(case["report_md"], case["sources"])
        assert result["total_citations"] == 3  # [1][2][3]；[2026] 年份不算
        assert result["unique_cited"] == 3
        assert result["matched"] == 3
        assert result["accuracy"] == 1.0
        assert result["unmatched_citations"] == []
        # 唯一未被引用的是 SQL 来源（正文与参考章节均未出现）
        assert len(result["uncited_sources"]) == 1
        assert result["uncited_sources"][0].startswith("sql:")

    def test_partial_match_fixture(self):
        case = _load_fixture("case_partial_cited.json")
        result = score_citations(case["report_md"], case["sources"])
        assert result["total_citations"] == 3
        assert result["unique_cited"] == 3
        assert result["matched"] == 2
        assert result["accuracy"] == pytest.approx(2 / 3)
        assert result["unmatched_citations"] == ["5"]  # 参考章节无此条目，位置兜底也越界
        assert result["uncited_sources"] == [
            "docs: 药品销售白皮书.pdf",
            "sql: SELECT drug_name, SUM(quantity_on_hand) AS total FROM inventory "
            "GROUP BY drug_name ORDER BY total DESC LIMIT 10",
        ]

    def test_url_match_ignores_trailing_slash(self):
        report = "见 [1]\n\n## 【参考来源】\n\n1. [示例](https://example.com/a/)"
        sources = [{"type": "web", "title": "示例", "url_or_ref": "https://example.com/a"}]
        result = score_citations(report, sources)
        assert result["accuracy"] == 1.0
        assert result["unmatched_citations"] == []
        assert result["uncited_sources"] == []

    def test_empty_sources(self):
        report = "引用 [1] [2]，但没有任何登记来源。\n\n## 【参考来源】\n\n1. [A](https://e.com/a)"
        result = score_citations(report, [])
        assert result["total_citations"] == 2
        assert result["unique_cited"] == 2
        assert result["matched"] == 0
        assert result["accuracy"] == 0.0
        assert result["unmatched_citations"] == ["1", "2"]
        assert result["uncited_sources"] == []  # 没有登记来源，谈不上"未引用"

    def test_report_without_citations(self):
        sources = [{"type": "web", "title": "A", "url_or_ref": "https://e.com/a"}]
        result = score_citations("# 报告\n正文没有任何引用标记", sources)
        assert result["total_citations"] == 0
        assert result["unique_cited"] == 0
        assert result["matched"] == 0
        assert result["accuracy"] == 0.0  # 无引用 → accuracy=0.0（口径见模块 docstring）
        assert result["uncited_sources"] == ["web: A"]

    def test_empty_report_raises(self):
        # 空报告视为上游数据缺陷：显式抛 ValueError（口径见模块 docstring）
        for empty in ("", "   \n\t", None):
            with pytest.raises(ValueError):
                score_citations(empty, [])

    def test_positional_fallback_without_reference_section(self):
        report = "结论一 [1]，结论二 [2]。"
        sources = [
            {"type": "web", "title": "A", "url_or_ref": "https://e.com/a"},
            {"type": "docs", "title": "白皮书.pdf", "url_or_ref": "白皮书.pdf"},
        ]
        result = score_citations(report, sources)
        # 无参考章节时按统一编号位置兜底：[N] → sources[N-1]（口径见模块 docstring）
        assert result["matched"] == 2
        assert result["accuracy"] == 1.0
        assert result["unmatched_citations"] == []


class TestScoreCase:
    def test_with_token_usage(self):
        case = _load_fixture("case_cited_full.json")
        stats = score_case(case)
        assert stats["total_tokens"] == 128000
        assert stats["duration_s"] == 150.0
        assert stats["token_note"] == ""

    def test_token_null(self):
        case = _load_fixture("case_partial_cited.json")
        stats = score_case(case)
        assert stats["total_tokens"] is None
        assert "token_usage 为 null" in stats["token_note"]
        assert stats["duration_s"] == 90.5

    def test_total_missing_falls_back_to_input_output(self):
        stats = score_case(
            {"token_usage": {"input_tokens": 100, "output_tokens": 23}, "duration_s": 1.0}
        )
        assert stats["total_tokens"] == 123
        assert "input_tokens + output_tokens" in stats["token_note"]


class TestNearestRankP95:
    def test_small_sample_returns_max(self):
        # n=3 < 20 → ceil(0.95*3)=3 → 最大值（口径见 cost_latency docstring）
        assert nearest_rank_p95([30.0, 10.0, 20.0]) == 30.0

    def test_twenty_samples(self):
        # n=20 → r = ceil(0.95*20) = 19 → 升序第 19 名（0 基下标 18）
        assert nearest_rank_p95(list(range(1, 21))) == 19

    def test_empty(self):
        assert nearest_rank_p95([]) is None


class TestAggregate:
    def test_counts_tokens_and_durations(self):
        stats = [
            {"case_id": "a", "status": "completed", "duration_s": 10.0, "total_tokens": 100},
            {"case_id": "b", "status": "completed", "duration_s": 30.0, "total_tokens": None},
            {"case_id": "c", "status": "failed", "duration_s": 20.0, "total_tokens": 50},
            {"case_id": "d", "status": "timeout", "duration_s": None, "total_tokens": None},
            # 非 completed/failed 的标注态（如 make_report 的 missing）只计入 cases_run
            {"case_id": "e", "status": "missing", "duration_s": 5.0, "total_tokens": 10},
        ]
        agg = aggregate(stats)
        assert agg["cases_run"] == 5
        assert agg["cases_completed"] == 2
        assert agg["cases_failed"] == 2  # failed + timeout
        assert agg["total_tokens"] == 160  # None 跳过，已知值求和（偏低估计口径）
        assert agg["avg_duration_s"] == pytest.approx(65.0 / 4)
        assert agg["p95_duration_s"] == 30.0  # n=4 < 20 → 最大值

    def test_empty(self):
        agg = aggregate([])
        assert agg["cases_run"] == 0
        assert agg["cases_completed"] == 0
        assert agg["cases_failed"] == 0
        assert agg["total_tokens"] is None
        assert agg["avg_duration_s"] is None
        assert agg["p95_duration_s"] is None


class TestMakeReport:
    def _build_single_run_dir(self, tmp_path: Path) -> Path:
        """2 完成（全命中/部分命中+缺埋点）+ 1 失败 + 1 缺文件，附 summary.json"""
        run_dir = tmp_path / "results" / "run-fixture"
        run_dir.mkdir(parents=True)
        _write_case(run_dir, "case-001", _load_fixture("case_cited_full.json"))
        _write_case(run_dir, "case-002", _load_fixture("case_partial_cited.json"))
        failed_case = _load_fixture("case_cited_full.json") | {
            "case_id": "case-f3",
            "status": "failed",
            "error": "模型调用超时",
            "token_usage": {"input_tokens": 500, "output_tokens": 300, "total_tokens": 800},
            "duration_s": 12.0,
        }
        _write_case(run_dir, "case-f3", failed_case)
        (run_dir / "summary.json").write_text(
            (FIXTURE_DIR / "summary_fix_run.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return run_dir

    def test_single_run_report(self, tmp_path):
        run_dir = self._build_single_run_dir(tmp_path)
        out = tmp_path / "report.md"
        assert main(["--run-dir", str(run_dir), "--out", str(out)]) == 0
        text = out.read_text(encoding="utf-8")
        # 顶部汇总（run_id 取自 summary.json）
        assert "# 评测报告：run-fixture" in text
        assert "| cases | 4（完成 2 / 失败 1） |" in text
        assert "50.0%（2/4）" in text  # 完成率（case-m4 缺文件不计完成）
        assert "128800" in text  # token 合计：128000 + 800（缺埋点的 case-002 跳过）
        assert "偏低估计" in text  # 缺埋点提示
        assert "84.2 s" in text  # 平均耗时 (150 + 90.5 + 12) / 3
        assert "150.0 s" in text  # P95（n=3 < 20 → 最大值）
        assert "83.3%（5/6）" in text  # 整体引用准确率（微平均 5/6）
        assert "62.5%（5/8）" in text  # 引用覆盖率（5/8）
        # 每 case 明细
        assert "100.0%（3/3）" in text  # case-001 全命中
        assert "66.7%（2/3）" in text  # case-002 部分命中
        assert "case-f3 | failed" in text
        assert "未评分：status=failed" in text
        assert "error=模型调用超时" in text
        assert "case-m4" in text and "文件缺失" in text  # summary 登记但文件缺失
        assert "token_usage 为 null" in text  # case-002 缺埋点标注

    def test_two_run_comparison(self, tmp_path):
        dir_a = tmp_path / "run_a"
        dir_a.mkdir()
        _write_case(dir_a, "case-001", _load_fixture("case_cited_full.json"))
        dir_b = tmp_path / "run_b"
        dir_b.mkdir()
        _write_case(dir_b, "case-002", _load_fixture("case_partial_cited.json"))
        out = tmp_path / "compare.md"
        assert main(["--run-dir", str(dir_a), "--run-dir", str(dir_b), "--out", str(out)]) == 0
        text = out.read_text(encoding="utf-8")
        assert "# 评测对比：run_a → run_b" in text  # X→Y 口径，第一个为基线 X
        assert "差值（Y−X）" in text
        # 6 指标名与 docs/evaluation-metric-template.md 逐字对齐
        for metric in (
            "任务完成率",
            "数值准确率（DB 任务）",
            "引用覆盖率",
            "单任务 Token",
            "端到端耗时",
            "重复调用率",
        ):
            assert f"| {metric} " in text
        assert "| 数值准确率（DB 任务） | — | — | — |" in text  # W1 骨架不产出
        assert "| 重复调用率 | — | — | — |" in text
        assert "-25.0pp" in text  # 引用覆盖率 50.0% - 75.0%
        assert "-33.3pp" in text  # 引用准确率 66.7% - 100.0%
        assert "-59.5 / -59.5" in text  # 平均与 P95 时延差
        assert "| 单任务 Token | 128000 | — | — |" in text  # run_b 无埋点 → 差值缺失
        assert "## run_a 每 case 明细" in text
        assert "## run_b 每 case 明细" in text

    def test_missing_summary_and_bad_json(self, tmp_path):
        run_dir = tmp_path / "run_nosum"
        run_dir.mkdir()
        _write_case(run_dir, "case-001", _load_fixture("case_cited_full.json"))
        (run_dir / "case_bad.json").write_text("{not-json", encoding="utf-8")
        out = tmp_path / "report2.md"
        assert main(["--run-dir", str(run_dir), "--out", str(out)]) == 0
        text = out.read_text(encoding="utf-8")
        assert "# 评测报告：run_nosum" in text  # 无 summary 时 run_id 取目录名
        assert "JSON 解析失败" in text  # 坏文件标注并跳过
        assert "| cases | 2（完成 1 / 失败 0） |" in text  # 坏文件只计入 cases_run
        assert "50.0%（1/2）" in text  # 完成率（坏文件既不计完成也不计失败）

    def test_empty_run_dir(self, tmp_path):
        run_dir = tmp_path / "run_empty"
        run_dir.mkdir()
        out = tmp_path / "report3.md"
        assert main(["--run-dir", str(run_dir), "--out", str(out)]) == 0
        text = out.read_text(encoding="utf-8")
        assert "| cases | 0（完成 0 / 失败 0） |" in text
        assert "任务完成率 | —" in text

    def test_load_run_flags_missing_file_from_summary(self, tmp_path):
        run_dir = tmp_path / "run_missing"
        run_dir.mkdir()
        (run_dir / "summary.json").write_text(
            (FIXTURE_DIR / "summary_fix_run.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        run = load_run(run_dir)
        assert run["run_id"] == "run-fixture"  # summary 有 summary.json 时优先取其 run_id
        statuses = {row["case_id"]: row["status"] for row in run["rows"]}
        assert statuses["case-m4"] == "missing"  # summary 登记但文件缺失

    def test_run_dir_arg_validation(self, tmp_path):
        with pytest.raises(SystemExit):
            main([])  # 未提供 --run-dir
        with pytest.raises(SystemExit):
            main(["--run-dir", str(tmp_path)] * 3)  # 超过 2 个
        with pytest.raises(SystemExit):
            main(["--run-dir", str(tmp_path / "nope")])  # 目录不存在


# ---------------------------------------------------------------------------
# 引用评分 v2 口径（基线重算迭代）：裸 URL 抽取、URL 归一化增强、SQL 表名匹配
# ---------------------------------------------------------------------------


class TestCitationScoringV2:
    """v2 口径的防回归用例：救回形式差异、保留真实失配"""

    def test_bare_url_in_plain_text_entry_matches(self):
        """条目为"标题 - URL"纯文本写法（非 Markdown 链接）也应命中"""
        report = (
            "# 报告\n结论 [1]\n\n## 【参考来源】\n\n"
            "1. 人工智能法案官方实施时间表 - https://example.com/ai-act-timeline\n"
        )
        sources = [
            {"type": "web", "title": "AI Act Timeline", "url_or_ref": "https://example.com/ai-act-timeline"}
        ]
        result = score_citations(report, sources)
        assert result["accuracy"] == 1.0

    def test_percent_encoded_url_unifies_with_raw_chinese(self):
        """模型抄中文原文 URL、登记侧 percent-encoded：同一资源应命中"""
        report = (
            "# 报告\n结论 [1]\n\n## 【参考来源】\n\n"
            "1. [标题](https://www.idc.com/blog/人形机器人洞察)\n"
        )
        sources = [
            {
                "type": "web",
                "title": "IDC 洞察",
                "url_or_ref": "https://www.idc.com/blog/%E4%BA%BA%E5%BD%A2%E6%9C%BA%E5%99%A8%E4%BA%BA%E6%B4%9E%E5%AF%9F",
            }
        ]
        assert score_citations(report, sources)["accuracy"] == 1.0

    def test_tracking_params_and_order_ignored(self):
        """跟踪参数剔除 + 查询参数排序：形式变体视为同一 URL"""
        report = "# 报告\n[1]\n\n## 参考来源\n\n1. [标题](https://example.com/p?b=2&a=1&fbclid=x9)\n"
        sources = [
            {"type": "web", "title": "t", "url_or_ref": "https://example.com/p?a=1&b=2"}
        ]
        assert score_citations(report, sources)["accuracy"] == 1.0

    def test_queryless_side_falls_back_to_host_path(self):
        """单侧无查询串：模型抄写丢参数视为同一资源（host+path 退化比对）"""
        report = "# 报告\n[1]\n\n## 参考来源\n\n1. [标题](https://example.com/article/9)\n"
        sources = [
            {"type": "web", "title": "t", "url_or_ref": "https://example.com/article/9?from=rss"}
        ]
        assert score_citations(report, sources)["accuracy"] == 1.0

    def test_both_sides_with_query_must_match_exactly(self):
        """两侧都带查询串则必须归一化全等：?id=1 与 ?id=2 是不同页面"""
        report = "# 报告\n[1]\n\n## 参考来源\n\n1. [标题](https://example.com/article?id=2)\n"
        sources = [
            {"type": "web", "title": "t", "url_or_ref": "https://example.com/article?id=1"}
        ]
        result = score_citations(report, sources)
        assert result["accuracy"] == 0.0

    def test_sql_source_matches_by_table_name(self):
        """SQL 来源按表名匹配转述文字：v1 的原文包含口径必然误判为 0"""
        report = (
            "# 报告\n[1]\n\n## 【参考来源】\n\n"
            "1. 数据库查询：sales_records 表按月聚合销售额，筛选 2025 年\n"
        )
        sources = [
            {
                "type": "sql",
                "title": "SQL 查询记录",
                "url_or_ref": "SELECT strftime('%m', sale_date) AS m, SUM(total_amount) FROM sales_records GROUP BY m",
            }
        ]
        assert score_citations(report, sources)["accuracy"] == 1.0

    def test_sql_source_without_table_reference_stays_unmatched(self):
        """条目连查询对象（表名）都没写：真实失配，不救"""
        report = "# 报告\n[1]\n\n## 【参考来源】\n\n1. 数据库查询助手查询结果：全年汇总数据。\n"
        sources = [
            {
                "type": "sql",
                "title": "SQL 查询记录",
                "url_or_ref": "SELECT sale_id, sale_date FROM sales_records",
            }
        ]
        assert score_citations(report, sources)["accuracy"] == 0.0

    def test_genuinely_mismatched_url_not_rescued(self):
        """张冠李戴（条目 URL 与任何登记来源不同站）不因归一化增强被误救"""
        report = "# 报告\n[1]\n\n## 参考来源\n\n1. [标题](https://news.ustc.edu.cn/info/1048/93556.htm)\n"
        sources = [
            {"type": "web", "title": "t", "url_or_ref": "https://www.stdaily.com/web/gjxw/content_473247.html"}
        ]
        assert score_citations(report, sources)["accuracy"] == 0.0
