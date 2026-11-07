"""近窗细化的纯规则（零 IO）：窗口、触发、子段校验、幂等键。

这些函数不碰数据库，用固定时间对拍即可；真 PostgreSQL 上的行为
（CAS、预算、调度接线）在 `tests/integration/test_schedule_refine.py`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ling.domains.agenda.timeline import (
    REFINE_MAX_CHILDREN,
    Anchor,
    CoarseSegment,
    RefineValidationError,
    parse_refine_output,
    refine_key,
    refine_trigger_due,
    refine_window,
    validate_refine_children,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAY = datetime(2026, 11, 8, tzinfo=SHANGHAI)


def _local(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


def _parent(
    *, start: datetime | None = None, end: datetime | None = None, layer: str = "block"
) -> CoarseSegment:
    return CoarseSegment(
        segment_id="seg_parent",
        layer=layer,
        kind="work",
        title="工作",
        start_at=start or _local(9),
        end_at=end or _local(11),
    )


def _child(
    start: datetime,
    end: datetime,
    *,
    layer: str = "hour",
    title: str = "干活",
    kind: str = "work",
    place_id: str | None = None,
) -> dict[str, object]:
    return {
        "layer": layer,
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "title": title,
        "kind": kind,
        "place_id": place_id,
    }


# ---------------------------------------------------------------- 窗口


def test_window_clipped_to_future_and_parent() -> None:
    """窗口 = [max(now, 父段起点), min(父段终点, now+3h))。"""
    bounds = refine_window(_local(8), start_at=_local(9), end_at=_local(11))
    assert bounds == (_local(9), _local(11))


def test_window_never_backfills_past_segment_start() -> None:
    """现在已经在父段中间：起点是 now（向上取整到分钟），不是父段起点。"""
    bounds = refine_window(
        _local(10, 29) + timedelta(seconds=40),
        start_at=_local(9),
        end_at=_local(11),
    )
    # 10:29:40 向上取整到 10:30：既不会落进过去，也不会让同一分钟的键抖动
    assert bounds == (_local(10, 30), _local(11))
    # 再晚一分钟，剩余不足 30 分钟 → 不细化
    assert (
        refine_window(
            _local(10, 30) + timedelta(seconds=20),
            start_at=_local(9),
            end_at=_local(11),
        )
        is None
    )


def test_window_capped_at_three_hours() -> None:
    """父段很长时只看未来 3 小时。"""
    bounds = refine_window(_local(8), start_at=_local(9), end_at=_local(20))
    assert bounds == (_local(9), _local(11))


def test_window_refuses_tiny_remainder() -> None:
    """剩余不足 30 分钟就不细化（不为填碎片调模型）。"""
    assert refine_window(_local(10, 45), start_at=_local(9), end_at=_local(11)) is None


def test_window_refuses_past_parent() -> None:
    """父段已经结束：过去永不回填。"""
    assert refine_window(_local(12), start_at=_local(9), end_at=_local(11)) is None


def test_trigger_requires_within_sixty_minutes() -> None:
    assert refine_trigger_due(_local(8), start_at=_local(9)) is True
    assert refine_trigger_due(_local(8), start_at=_local(9, 1)) is False
    assert refine_trigger_due(_local(9, 30), start_at=_local(9)) is True


# ---------------------------------------------------------------- 幂等键


def test_refine_key_is_minute_precision_utc() -> None:
    """键里的窗口起点必须取分钟精度 UTC：同一分钟的重复扫描落在同一个键上。"""
    first = refine_key("dayplan_abc", 3, _local(9))
    second = refine_key(
        "dayplan_abc", 3, _local(9).astimezone(UTC) + timedelta(seconds=59)
    )
    assert first == second == "refine:dayplan_abc:3:20261108T0100Z"
    assert refine_key("dayplan_abc", 4, _local(9)) != first


# ---------------------------------------------------------------- 输出解析


def test_parse_rejects_non_json_and_unknown_keys() -> None:
    with pytest.raises(RefineValidationError):
        parse_refine_output("")
    with pytest.raises(RefineValidationError):
        parse_refine_output("不是 JSON")
    with pytest.raises(RefineValidationError):
        parse_refine_output('{"parent_id": "seg_x", "children": [], "extra": 1}')
    with pytest.raises(RefineValidationError):
        parse_refine_output('{"children": []}')
    with pytest.raises(RefineValidationError):
        parse_refine_output('{"parent_id": "seg_x", "children": {}}')

    parent_id, children = parse_refine_output(
        '{"parent_id": "seg_x", "children": []}'
    )
    assert parent_id == "seg_x" and children == []


# ---------------------------------------------------------------- 子段校验


def _validate(children: list[object], **overrides: object):  # noqa: ANN202
    parent = overrides.pop("parent", _parent())
    return validate_refine_children(
        children,
        parent=parent,  # type: ignore[arg-type]
        child_layer=overrides.pop("child_layer", "hour"),  # type: ignore[arg-type]
        window_start=overrides.pop("window_start", _local(9)),  # type: ignore[arg-type]
        window_end=overrides.pop("window_end", _local(11)),  # type: ignore[arg-type]
        anchors=overrides.pop("anchors", ()),  # type: ignore[arg-type]
        valid_place_ids=overrides.pop("valid_place_ids", frozenset()),  # type: ignore[arg-type]
    )


def test_valid_children_are_sorted_and_kept() -> None:
    children = _validate(
        [
            _child(_local(10), _local(11), title="收尾"),
            _child(_local(9), _local(10), title="开工"),
        ]
    )
    assert [item.title for item in children] == ["开工", "收尾"]
    assert all(item.layer == "hour" for item in children)


def test_partial_coverage_is_allowed() -> None:
    """不要求铺满父块：没想好的部分保持粗粒度。"""
    children = _validate([_child(_local(9), _local(9, 30))])
    assert len(children) == 1


def test_overlapping_siblings_rejected() -> None:
    with pytest.raises(RefineValidationError, match="重叠"):
        _validate(
            [
                _child(_local(9), _local(10, 30)),
                _child(_local(10), _local(11)),
            ]
        )


def test_child_outside_parent_rejected() -> None:
    with pytest.raises(RefineValidationError, match="父块"):
        _validate([_child(_local(11), _local(12))])


def test_child_outside_window_rejected() -> None:
    """父块 09:00–11:00，但窗口只有 09:00–10:00：10:00 之后的子段越窗。"""
    with pytest.raises(RefineValidationError, match="未来窗口"):
        _validate(
            [_child(_local(10), _local(11))],
            window_start=_local(9),
            window_end=_local(10),
        )


def test_too_many_children_rejected() -> None:
    children = [
        _child(_local(9) + timedelta(minutes=index * 10), _local(9) + timedelta(minutes=index * 10 + 9))
        for index in range(REFINE_MAX_CHILDREN + 1)
    ]
    with pytest.raises(RefineValidationError, match="超过上限"):
        _validate(children)


def test_wrong_layer_rejected() -> None:
    """递归层级硬上限：父层 block 只能产出 hour，minute 要等下一轮。"""
    with pytest.raises(RefineValidationError, match="layer"):
        _validate([_child(_local(9), _local(10), layer="minute")])


def test_too_short_child_rejected() -> None:
    with pytest.raises(RefineValidationError, match="下限"):
        _validate([_child(_local(9), _local(9) + timedelta(minutes=4))])


def test_naive_or_second_precision_rejected() -> None:
    with pytest.raises(RefineValidationError, match="时区"):
        _validate(
            [
                {
                    "layer": "hour",
                    "start_at": "2026-11-08T09:00:00",
                    "end_at": "2026-11-08T10:00:00",
                    "title": "干活",
                    "kind": "work",
                    "place_id": None,
                }
            ]
        )
    with pytest.raises(RefineValidationError, match="分钟"):
        _validate(
            [
                {
                    "layer": "hour",
                    "start_at": _local(9).replace(second=30).isoformat(),
                    "end_at": _local(10).isoformat(),
                    "title": "干活",
                    "kind": "work",
                    "place_id": None,
                }
            ]
        )


def test_claim_marker_and_unknown_fields_rejected() -> None:
    with pytest.raises(RefineValidationError, match="完成或承诺"):
        _validate([_child(_local(9), _local(10), title="已经写完了")])
    bad = _child(_local(9), _local(10))
    bad["extra"] = "x"
    with pytest.raises(RefineValidationError, match="未定义字段"):
        _validate([bad])
    with pytest.raises(RefineValidationError, match="kind"):
        _validate([_child(_local(9), _local(10), kind="sleep")])


def test_place_must_be_known() -> None:
    with pytest.raises(RefineValidationError, match="未知地点"):
        _validate([_child(_local(9), _local(10), place_id="place_x")])
    assert _validate(
        [_child(_local(9), _local(10), place_id="place_home")],
        valid_place_ids=frozenset({"place_home"}),
    )


def test_anchor_overlap_rejected() -> None:
    anchor = Anchor(
        kind="commitment",
        start_at=_local(9, 30),
        end_at=_local(10),
        title="约定：见个面",
    )
    with pytest.raises(RefineValidationError, match="锚点"):
        _validate([_child(_local(9), _local(10))], anchors=(anchor,))


def test_empty_children_is_not_an_error_here() -> None:
    """校验层返回空列表；"空结果算失败"由调用方（refine_work）判。"""
    assert _validate([]) == []
