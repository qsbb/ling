"""Activity 响应契约：``_activity_dict`` 必须透出 ``ActivityView.origin``。

回归 B-1：生活页 `/life` 的来源徽标读 `current.origin`。后端漏这个键时，
前端 `ORIGIN_LABEL[undefined] ?? undefined` 会渲染成一个没有文字的描边胶囊
（视觉验收 mobile-05-life 实测）。这里锁死键存在且值来自 ActivityView。
"""

from __future__ import annotations

from ling.api.routes.manage import _activity_dict
from ling.domains.activity.service import ActivityView


def _view(**overrides: object) -> ActivityView:
    fields: dict[str, object] = {
        "activity_id": "act_test",
        "kind": "reading",
        "state": "running",
        "place_id": None,
    }
    fields.update(overrides)
    return ActivityView(**fields)  # type: ignore[arg-type]


def test_activity_dict_exposes_origin_key() -> None:
    view = _view(origin="tool_receipt")

    payload = _activity_dict(view)

    assert "origin" in payload
    assert payload["origin"] == view.origin == "tool_receipt"


def test_activity_dict_origin_defaults_to_simulated() -> None:
    view = _view()

    assert view.origin == "simulated"
    assert _activity_dict(view)["origin"] == "simulated"
