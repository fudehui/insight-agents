"""
evals/loader.py 的单元测试

覆盖：合法加载（含可选字段）、真实 w1_batch.yaml / w2_batch.yaml 批次、缺字段/
类型错/取值越界的可读报错、load_cases_multi 多文件合并与跨文件 id 冲突、以及
filter_cases 的 ids/limit 过滤逻辑。不依赖任何外部服务。
"""

from pathlib import Path

import pytest
import yaml

from evals.loader import (
    VALID_SOURCE_TYPES,
    CaseValidationError,
    GoldenCase,
    filter_cases,
    load_cases,
    load_cases_multi,
)

# 真实批次文件：由本测试保证它们与 schema 始终一致
W1_BATCH_PATH = Path(__file__).resolve().parents[1] / "evals" / "golden" / "w1_batch.yaml"
W2_BATCH_PATH = Path(__file__).resolve().parents[1] / "evals" / "golden" / "w2_batch.yaml"


def _write_cases(tmp_path, cases, filename="cases.yaml"):
    """把用例列表写成 YAML 文件，供 load_cases 加载"""
    path = tmp_path / filename
    path.write_text(
        yaml.safe_dump({"cases": cases}, allow_unicode=True), encoding="utf-8"
    )
    return path


def _valid_case(case_id="c-001", **overrides):
    """构造一条合法用例，overrides 覆盖任意字段"""
    case = {
        "id": case_id,
        "query": "测试问题：查询库存并生成报告",
        "expected_points": ["要点一", "要点二", "要点三"],
        "expected_tools": ["execute_sql_query", "generate_markdown"],
        "expected_source_types": ["sql"],
    }
    case.update(overrides)
    return case


# ---------------------------------------------------------------------------
# 合法加载
# ---------------------------------------------------------------------------


def test_load_valid_cases(tmp_path):
    path = _write_cases(tmp_path, [_valid_case("a"), _valid_case("b")])

    cases = load_cases(path)

    assert isinstance(cases, list) and len(cases) == 2
    assert all(isinstance(case, GoldenCase) for case in cases)
    assert [case.id for case in cases] == ["a", "b"]
    first = cases[0]
    assert first.query == "测试问题：查询库存并生成报告"
    assert first.expected_points == ["要点一", "要点二", "要点三"]
    assert first.expected_tools == ["execute_sql_query", "generate_markdown"]
    assert first.expected_source_types == ["sql"]
    # 可选字段缺省值
    assert first.tags == []
    assert first.notes == ""


def test_load_valid_case_with_optional_fields(tmp_path):
    path = _write_cases(
        tmp_path,
        [
            _valid_case(
                "a",
                tags=["boundary", "budget"],
                notes="设计为触发预算拦截",
            )
        ],
    )

    case = load_cases(path)[0]

    assert case.tags == ["boundary", "budget"]
    assert case.notes == "设计为触发预算拦截"


def test_load_real_w1_batch():
    """真实批次必须始终通过 schema：10 条、id 唯一、覆盖边界用例"""
    cases = load_cases(W1_BATCH_PATH)

    assert len(cases) == 10
    ids = [case.id for case in cases]
    assert ids == [f"w1-{i:03d}" for i in range(1, 11)]
    assert len(set(ids)) == 10

    for case in cases:
        assert 3 <= len(case.expected_points) <= 5
        assert case.expected_tools
        assert case.expected_source_types
        assert all(s in VALID_SOURCE_TYPES for s in case.expected_source_types)
        assert case.query.strip()

    # 覆盖要求：web 2 / sql 2 / ragflow 2 / 混合 2 / 边界 2
    boundary = [case for case in cases if "boundary" in case.tags]
    assert len(boundary) == 2
    mixed = [case for case in cases if set(case.expected_source_types) == {"sql", "web"}]
    assert len(mixed) == 2


def test_load_real_w2_batch():
    """真实 W2 批次必须始终通过 schema：20 条、id 唯一、分布与 W1 不重叠"""
    cases = load_cases(W2_BATCH_PATH)

    assert len(cases) == 20
    ids = [case.id for case in cases]
    assert ids == [f"w2-{i:03d}" for i in range(1, 21)]
    assert len(set(ids)) == 20

    for case in cases:
        assert 3 <= len(case.expected_points) <= 5
        assert case.expected_tools
        assert case.expected_source_types
        assert all(s in VALID_SOURCE_TYPES for s in case.expected_source_types)
        assert case.query.strip()

    # 分布要求：web 6 / db 5 / ragflow 4 / mixed 3 / boundary 2
    def _count(tag: str) -> int:
        return len([case for case in cases if tag in case.tags])

    assert _count("web") == 6
    assert _count("db") == 5
    assert _count("ragflow") == 4
    assert _count("mixed") == 3
    assert _count("boundary") == 2

    # db 类用例必须以 sql 为来源类型（数值真值可核对的前提）
    for case in cases:
        if "db" in case.tags:
            assert "sql" in case.expected_source_types


# ---------------------------------------------------------------------------
# load_cases_multi（--cases 多文件合并）
# ---------------------------------------------------------------------------


def test_load_cases_multi_merges_in_order(tmp_path):
    first = _write_cases(tmp_path, [_valid_case("a-1"), _valid_case("a-2")], filename="a.yaml")
    second = _write_cases(tmp_path, [_valid_case("b-1")], filename="b.yaml")

    merged = load_cases_multi([first, second])

    assert [case.id for case in merged] == ["a-1", "a-2", "b-1"]
    assert all(isinstance(case, GoldenCase) for case in merged)


def test_load_cases_multi_duplicate_id_across_files(tmp_path):
    """跨文件 id 重复报 CaseValidationError，报错给出 id 与两个来源文件"""
    first = _write_cases(tmp_path, [_valid_case("dup")], filename="a.yaml")
    second = _write_cases(tmp_path, [_valid_case("dup"), _valid_case("ok")], filename="b.yaml")

    with pytest.raises(CaseValidationError) as excinfo:
        load_cases_multi([first, second])
    message = str(excinfo.value)
    assert "跨文件 id 重复：'dup'" in message
    assert "a.yaml" in message and "b.yaml" in message


def test_load_cases_multi_real_batches_30_unique():
    """W1+W2 两个真实批次合并加载：30 条、按序拼接、无 id 交叉重复"""
    merged = load_cases_multi([W1_BATCH_PATH, W2_BATCH_PATH])

    assert len(merged) == 30
    ids = [case.id for case in merged]
    assert len(set(ids)) == 30
    assert ids[:10] == [f"w1-{i:03d}" for i in range(1, 11)]
    assert ids[10:] == [f"w2-{i:03d}" for i in range(1, 21)]


def test_load_cases_multi_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_cases_multi([W1_BATCH_PATH, tmp_path / "no_such.yaml"])


# ---------------------------------------------------------------------------
# 校验失败场景（报错信息可读性）
# ---------------------------------------------------------------------------


def test_missing_required_field_reports_field_name(tmp_path):
    case = _valid_case()
    del case["expected_tools"]
    path = _write_cases(tmp_path, [case])

    with pytest.raises(CaseValidationError) as excinfo:
        load_cases(path)
    message = str(excinfo.value)
    assert "缺少必填字段 'expected_tools'" in message
    assert "c-001" in message  # 报错要能定位到具体 case


def test_wrong_type_reports_readable_error(tmp_path):
    # expected_points 写成了字符串而非列表
    path = _write_cases(tmp_path, [_valid_case(expected_points="要点一；要点二")])

    with pytest.raises(CaseValidationError) as excinfo:
        load_cases(path)
    assert "'expected_points' 必须是非空字符串列表" in str(excinfo.value)


def test_non_string_list_item_reports_error(tmp_path):
    path = _write_cases(tmp_path, [_valid_case(expected_tools=[1, 2])])

    with pytest.raises(CaseValidationError) as excinfo:
        load_cases(path)
    assert "'expected_tools' 必须是非空字符串列表" in str(excinfo.value)


def test_expected_points_out_of_range(tmp_path):
    path = _write_cases(
        tmp_path, [_valid_case(expected_points=["只有", "两条"])]
    )

    with pytest.raises(CaseValidationError) as excinfo:
        load_cases(path)
    assert "需要 3-5 条，实际 2 条" in str(excinfo.value)


def test_invalid_source_type(tmp_path):
    path = _write_cases(
        tmp_path, [_valid_case(expected_source_types=["web", "mysql"])]
    )

    with pytest.raises(CaseValidationError) as excinfo:
        load_cases(path)
    assert "非法取值 ['mysql']" in str(excinfo.value)


def test_duplicate_id(tmp_path):
    path = _write_cases(tmp_path, [_valid_case("dup"), _valid_case("dup")])

    with pytest.raises(CaseValidationError) as excinfo:
        load_cases(path)
    assert "id 重复：'dup'" in str(excinfo.value)


def test_empty_query(tmp_path):
    path = _write_cases(tmp_path, [_valid_case(query="   ")])

    with pytest.raises(CaseValidationError) as excinfo:
        load_cases(path)
    assert "'query' 必须是非空字符串" in str(excinfo.value)


def test_case_not_a_dict(tmp_path):
    path = _write_cases(tmp_path, ["not-a-dict"])

    with pytest.raises(CaseValidationError) as excinfo:
        load_cases(path)
    assert "必须是映射（dict）" in str(excinfo.value)


def test_invalid_top_level_structure(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump([_valid_case()]), encoding="utf-8")

    with pytest.raises(CaseValidationError) as excinfo:
        load_cases(path)
    assert "顶层结构" in str(excinfo.value)


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_cases(tmp_path / "no_such_file.yaml")


def test_errors_are_aggregated(tmp_path):
    """多处错误一次性汇总报出，方便用例作者一次改完"""
    first = _valid_case("dup")
    del first["query"]
    second = _valid_case("dup")
    second["expected_source_types"] = ["oracle"]
    path = _write_cases(tmp_path, [first, second])

    with pytest.raises(CaseValidationError) as excinfo:
        load_cases(path)
    message = str(excinfo.value)
    assert "缺少必填字段 'query'" in message
    assert "id 重复：'dup'" in message
    assert "非法取值 ['oracle']" in message


# ---------------------------------------------------------------------------
# filter_cases
# ---------------------------------------------------------------------------


def _sample_cases():
    return [GoldenCase(id=f"c-{i}", query="q", expected_points=["p"] * 3,
                       expected_tools=["t"], expected_source_types=["web"])
            for i in range(1, 6)]


def test_filter_by_ids_preserves_yaml_order():
    cases = _sample_cases()

    # 传入顺序打乱，返回仍按 YAML 原有顺序
    selected = filter_cases(cases, ids=["c-4", "c-1"])
    assert [case.id for case in selected] == ["c-1", "c-4"]


def test_filter_unknown_id_raises_with_available_ids():
    cases = _sample_cases()

    with pytest.raises(ValueError) as excinfo:
        filter_cases(cases, ids=["c-1", "nope"])
    message = str(excinfo.value)
    assert "nope" in message
    assert "c-1, c-2, c-3, c-4, c-5" in message


def test_filter_limit_truncates_after_ids():
    cases = _sample_cases()

    # 先过滤再截断：c-3/c-4/c-5 过滤后取前 2 条
    selected = filter_cases(cases, ids=["c-3", "c-4", "c-5"], limit=2)
    assert [case.id for case in selected] == ["c-3", "c-4"]

    assert [case.id for case in filter_cases(cases, limit=3)] == ["c-1", "c-2", "c-3"]


def test_filter_limit_zero_returns_empty():
    assert filter_cases(_sample_cases(), limit=0) == []


def test_filter_negative_limit_raises():
    with pytest.raises(ValueError):
        filter_cases(_sample_cases(), limit=-1)


def test_filter_no_args_returns_all_in_order():
    cases = _sample_cases()
    assert filter_cases(cases) == cases
