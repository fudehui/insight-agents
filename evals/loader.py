"""
评测 golden case 加载与 schema 校验模块

把 evals/golden/*.yaml 中的任务用例加载为 GoldenCase 数据类，并在加载时
完成 schema 校验：缺字段、类型错误、取值越界都汇总为包含 case 序号与 id
的可读报错，避免跑批执行到一半才发现用例写错。只依赖 pyyaml + dataclasses，
不引入新依赖。

schema 约束（YAML 顶层为 cases 列表，每条 case 字段如下）：
- id:                    必填，非空字符串，单个文件内唯一
- query:                 必填，非空字符串，作为 task_query 喂给 run_deep_agent
- expected_points:       必填，3-5 条非空字符串，人工标注的可判定要点
- expected_tools:        必填，非空字符串列表，预期应调用的工具注册名
                         （与 app/api/budget.py DEFAULT_TOOL_LIMITS 及各 @tool 名一致）
- expected_source_types: 必填，非空列表且取值 ⊆ {web, sql, ragflow, doc}
- tags:                  可选，字符串列表（如 boundary / db / web）
- notes:                 可选，字符串，用例设计说明（如真值 SQL、预期触发行为）

多文件合并：load_cases_multi 按传参顺序加载多个 YAML 并拼接，跨文件 id
重复一次性汇总报错（run_eval 的 --cases 可重复传参即基于此）。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml

# expected_source_types 的合法取值（输出契约中 sources[].type 同源）
VALID_SOURCE_TYPES = ("web", "sql", "ragflow", "doc")

# expected_points 的条数区间：太少不可判定，太多难以人工维护
_MIN_EXPECTED_POINTS = 3
_MAX_EXPECTED_POINTS = 5

_REQUIRED_FIELDS = (
    "id",
    "query",
    "expected_points",
    "expected_tools",
    "expected_source_types",
)


@dataclasses.dataclass
class GoldenCase:
    """单条评测用例（与 YAML 字段一一对应）"""

    id: str
    query: str
    expected_points: list[str]
    expected_tools: list[str]
    expected_source_types: list[str]
    tags: list[str] = dataclasses.field(default_factory=list)
    notes: str = ""


class CaseValidationError(ValueError):
    """golden case YAML 不符合 schema 时抛出，message 面向用例作者可直接阅读"""


def load_cases(path: str | Path) -> list[GoldenCase]:
    """
    加载并校验 golden case YAML 文件

    :param path: YAML 文件路径
    :return: 按文件中书写顺序排列的 GoldenCase 列表
    :raises FileNotFoundError: 文件不存在
    :raises CaseValidationError: YAML 解析失败或 schema 校验不通过（一次性汇总全部错误）
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"用例文件不存在：{file_path}")

    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise CaseValidationError(f"YAML 解析失败（{file_path}）：{e}") from e

    if not isinstance(raw, dict) or "cases" not in raw:
        raise CaseValidationError(
            f"顶层结构必须是包含 'cases' 键的映射，实际为 {type(raw).__name__}"
        )
    raw_cases = raw["cases"]
    if not isinstance(raw_cases, list):
        raise CaseValidationError(
            f"'cases' 必须是列表，实际为 {type(raw_cases).__name__}"
        )

    errors: list[str] = []
    cases: list[GoldenCase] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_cases, start=1):
        case_errors, case = _validate_case(item, index, seen_ids)
        errors.extend(case_errors)
        if case is not None:
            cases.append(case)

    if errors:
        raise CaseValidationError(
            f"用例文件校验失败（共 {len(errors)} 处）：\n- " + "\n- ".join(errors)
        )
    return cases


def load_cases_multi(paths: list[str | Path]) -> list[GoldenCase]:
    """
    加载并合并多个 golden case YAML 文件（按传参顺序拼接，供 --cases 多次传参）

    :param paths: YAML 文件路径列表（允许重复传同一文件以外的任意组合）
    :return: 按传参顺序拼接的 GoldenCase 列表
    :raises FileNotFoundError: 任一文件不存在
    :raises CaseValidationError: 任一文件 schema 校验不通过，或跨文件 id 重复
        （一次性汇总全部冲突，报错同时给出两个来源文件）
    """
    merged: list[GoldenCase] = []
    seen: dict[str, Path] = {}
    errors: list[str] = []

    for path in paths:
        file_path = Path(path)
        # 单文件校验复用 load_cases：缺字段/类型错等仍按原报错格式抛出
        for case in load_cases(file_path):
            if case.id in seen:
                errors.append(
                    f"跨文件 id 重复：'{case.id}' 同时出现在 "
                    f"{seen[case.id]} 与 {file_path}"
                )
                continue
            seen[case.id] = file_path
            merged.append(case)

    if errors:
        raise CaseValidationError(
            f"用例文件合并失败（共 {len(errors)} 处）：\n- " + "\n- ".join(errors)
        )
    return merged


def _validate_case(
    item: object, index: int, seen_ids: set[str]
) -> tuple[list[str], GoldenCase | None]:
    """
    校验单条 case，返回 (错误列表, 解析结果)

    :param item: YAML 中的一条 case（应为 dict）
    :param index: 条目序号（1 起），用于错误定位
    :param seen_ids: 已登记的 id 集合，用于唯一性校验
    :return: (errors, case)；errors 非空时 case 为 None
    """
    if not isinstance(item, dict):
        return [f"第 {index} 条 case：必须是映射（dict），实际为 {type(item).__name__}"], None

    errors: list[str] = []
    label = f"第 {index} 条 case（id={item.get('id')!r}）"

    for field_name in _REQUIRED_FIELDS:
        if field_name not in item:
            errors.append(f"{label} 缺少必填字段 '{field_name}'")

    # --- id：非空字符串且唯一 ---
    case_id = item.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        errors.append(f"{label} 的 'id' 必须是非空字符串")
    elif case_id in seen_ids:
        errors.append(f"{label} 的 id 重复：'{case_id}'")
    else:
        seen_ids.add(case_id)

    # --- query：非空字符串 ---
    query = item.get("query")
    if not isinstance(query, str) or not query.strip():
        errors.append(f"{label} 的 'query' 必须是非空字符串")

    # --- expected_points：3-5 条非空字符串 ---
    points = item.get("expected_points")
    if not isinstance(points, list) or not all(
        isinstance(p, str) and p.strip() for p in points
    ):
        errors.append(f"{label} 的 'expected_points' 必须是非空字符串列表")
    elif not (_MIN_EXPECTED_POINTS <= len(points) <= _MAX_EXPECTED_POINTS):
        errors.append(
            f"{label} 的 'expected_points' 需要 {_MIN_EXPECTED_POINTS}-"
            f"{_MAX_EXPECTED_POINTS} 条，实际 {len(points)} 条"
        )

    # --- expected_tools：非空字符串列表 ---
    tools = item.get("expected_tools")
    if (
        not isinstance(tools, list)
        or not tools
        or not all(isinstance(t, str) and t.strip() for t in tools)
    ):
        errors.append(f"{label} 的 'expected_tools' 必须是非空字符串列表")

    # --- expected_source_types：非空列表且取值合法 ---
    source_types = item.get("expected_source_types")
    if (
        not isinstance(source_types, list)
        or not source_types
        or not all(isinstance(s, str) for s in source_types)
    ):
        errors.append(f"{label} 的 'expected_source_types' 必须是非空字符串列表")
    else:
        invalid = [s for s in source_types if s not in VALID_SOURCE_TYPES]
        if invalid:
            errors.append(
                f"{label} 的 'expected_source_types' 含非法取值 {invalid}，"
                f"允许值：{list(VALID_SOURCE_TYPES)}"
            )

    # --- tags / notes：可选字段，给了就必须类型正确 ---
    tags = item.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        errors.append(f"{label} 的 'tags' 必须是字符串列表（或缺省）")

    notes = item.get("notes") or ""
    if not isinstance(notes, str):
        errors.append(f"{label} 的 'notes' 必须是字符串（或缺省）")

    if errors:
        return errors, None

    return [], GoldenCase(
        id=case_id,
        query=query,
        expected_points=list(points),
        expected_tools=list(tools),
        expected_source_types=list(source_types),
        tags=list(tags),
        notes=notes,
    )


def filter_cases(
    cases: list[GoldenCase],
    ids: list[str] | None = None,
    limit: int | None = None,
) -> list[GoldenCase]:
    """
    按条件筛选用例：先按 ids 过滤（保持 YAML 原有顺序），再按 limit 截断

    :param cases: load_cases 的返回结果
    :param ids: 指定要执行的 case id 列表；None 表示全部。含未知 id 时抛 ValueError
    :param limit: 最多保留条数；None 表示不截断。0 返回空列表，负数抛 ValueError
    :return: 筛选后的用例列表
    """
    selected = list(cases)

    if ids is not None:
        known_ids = {case.id for case in cases}
        # dict.fromkeys 去重的同时保持报错信息中的出现顺序
        unknown = [i for i in dict.fromkeys(ids) if i not in known_ids]
        if unknown:
            available = ", ".join(case.id for case in cases)
            raise ValueError(
                f"未知的 case id：{unknown}；当前用例集可用 id：{available}"
            )
        id_set = set(ids)
        selected = [case for case in selected if case.id in id_set]

    if limit is not None:
        if limit < 0:
            raise ValueError(f"limit 不能为负数，实际为 {limit}")
        selected = selected[:limit]

    return selected
