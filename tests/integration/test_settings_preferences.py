"""用户偏好（时区）的读写与生效链路。

## 为什么这些用例值得单独存在

用户报的现象是「设置里没有时区，生活页却说时区未设置、主动联系被整条停掉」。
那不是一处文案问题，而是三个环节一起错：

1. 没有持久化位置（只有环境变量）；
2. 没有设置面（网页上填不了）；
3. 总览「今天」与主动联系各自兜底成 UTC / None。

这里按链路验：拿默认值 → 改 → 回显 → 总览跟着变 → 主动联系不再因缺时区停。
"""

from __future__ import annotations

import pytest

from ling.infra.db import session_scope
from ling.infra.models import UserPreferenceRow
from ling.settings.user_preferences import timezone_by_subject
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def test_defaults_to_shanghai(logged_in: TestEnv) -> None:
    """没设过时返回默认值 Asia/Shanghai，并**说清**这是默认值。"""
    response = await logged_in.client.get("/v1/settings/preferences")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["timezone"] == "Asia/Shanghai"
    assert body["source"] == "default", "没设过却说成用户设的"
    assert body["stored"] is None
    assert body["env"] is None
    assert body["default"] == "Asia/Shanghai"
    assert "Asia/Shanghai" in body["common"]


async def test_put_rejects_non_iana_name(logged_in: TestEnv) -> None:
    """非法名字必须拒绝，并给出可执行的提示。

    "UTC+8" 是最常见的误解；只回一句 invalid，用户会以为是服务端 bug。
    """
    response = await logged_in.client.put(
        "/v1/settings/preferences", json={"timezone": "UTC+8"}
    )
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert "IANA" in error["message"] + (error.get("detail") or "")
    # 拒绝之后不能留下半个值
    after = (await logged_in.client.get("/v1/settings/preferences")).json()
    assert after["source"] == "default"


async def test_save_then_read_back(logged_in: TestEnv) -> None:
    """保存后回显：生效值是存的那个，来源是 preference。"""
    saved = await logged_in.client.put(
        "/v1/settings/preferences", json={"timezone": "Asia/Tokyo"}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["timezone"] == "Asia/Tokyo"
    assert saved.json()["source"] == "preference"

    body = (await logged_in.client.get("/v1/settings/preferences")).json()
    assert body["timezone"] == "Asia/Tokyo"
    assert body["stored"] == "Asia/Tokyo"
    assert body["source"] == "preference"


async def test_clear_falls_back_to_default(logged_in: TestEnv) -> None:
    """空串 = 清掉偏好（回退到环境变量/默认值），不是"设成空时区"。"""
    await logged_in.client.put(
        "/v1/settings/preferences", json={"timezone": "Asia/Tokyo"}
    )
    cleared = await logged_in.client.put("/v1/settings/preferences", json={"timezone": ""})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["stored"] is None
    assert cleared.json()["source"] == "default"
    assert cleared.json()["timezone"] == "Asia/Shanghai"


async def test_dashboard_today_boundary_uses_saved_timezone(logged_in: TestEnv) -> None:
    """总览的「今天」跟着保存的时区走，并报出来源。

    原来这里是 `settings.user_timezone or "UTC"`：设置里没有时区、总览按 UTC
    划日界，而她的免打扰按另一个时区算——两个"今天"不是同一天。
    """
    await logged_in.client.put(
        "/v1/settings/preferences", json={"timezone": "America/Los_Angeles"}
    )
    response = await logged_in.client.get("/v1/dashboard")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["timezone"] == "America/Los_Angeles"
    assert body["timezone_source"] == "preference"


async def test_proactive_no_longer_blocks_on_missing_timezone(logged_in: TestEnv) -> None:
    """生活页那句「用户时区未设置…已暂停主动联系」不该再出现。

    它是 `/v1/proactive` 的 gate_reasons 原文。默认时区下这条门已经不存在，
    只剩真正的理由（渠道、免打扰、日限额…）。
    """
    response = await logged_in.client.get("/v1/proactive")
    assert response.status_code == 200, response.text
    reasons = response.json()["gate_reasons"]
    assert not any("时区未设置" in reason for reason in reasons), reasons
    assert not any("LING_USER_TIMEZONE" in reason for reason in reasons), reasons


# ---------------------------------------------------------------- 两个偏好互不覆盖（B1）

# 两个偏好共用一条 PUT，前端各发各的（setTimezone 只带 timezone、setVideoMode
# 只带 video_mode）。缺省值一旦等于"清掉"，切一次视频模式就会静默清掉时区——
# 用户看到「已保存」，刷新后才发现回到默认。下面四条把两种方向的"只改一个"
# 和"两个都不发"都钉住。


async def test_video_mode_update_keeps_timezone(logged_in: TestEnv) -> None:
    """只发 video_mode：时区一个字节都不许动。"""
    await logged_in.client.put(
        "/v1/settings/preferences", json={"timezone": "Asia/Tokyo"}
    )
    response = await logged_in.client.put(
        "/v1/settings/preferences", json={"video_mode": "native"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["video_mode"] == "native"
    assert body["timezone"] == "Asia/Tokyo", "切视频模式把时区清掉了"
    assert body["source"] == "preference"
    assert body["stored"] == "Asia/Tokyo"


async def test_timezone_update_keeps_video_mode(logged_in: TestEnv) -> None:
    """只发 timezone：视频模式一个字节都不许动。"""
    await logged_in.client.put(
        "/v1/settings/preferences", json={"video_mode": "native"}
    )
    response = await logged_in.client.put(
        "/v1/settings/preferences", json={"timezone": "Asia/Tokyo"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["timezone"] == "Asia/Tokyo"
    assert body["video_mode"] == "native", "改时区把视频模式清掉了"


async def test_empty_body_changes_nothing(logged_in: TestEnv) -> None:
    """两个字段都不发 = 全都不变（不是"全清空"）。"""
    await logged_in.client.put(
        "/v1/settings/preferences",
        json={"timezone": "Asia/Tokyo", "video_mode": "native"},
    )
    response = await logged_in.client.put("/v1/settings/preferences", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["timezone"] == "Asia/Tokyo"
    assert body["source"] == "preference"
    assert body["video_mode"] == "native"


async def test_empty_string_clears_video_mode(logged_in: TestEnv) -> None:
    """显式空串 = 清掉（回退默认 frames），库里存的是 NULL 而不是 'frames'。

    区分"从没选过"和"选过又选回默认"：以后想改默认值，前者要跟着变，
    后者不该。所以清空写 NULL，不写默认值。
    """
    await logged_in.client.put(
        "/v1/settings/preferences", json={"video_mode": "native"}
    )
    response = await logged_in.client.put(
        "/v1/settings/preferences", json={"video_mode": ""}
    )
    assert response.status_code == 200, response.text
    assert response.json()["video_mode"] == "frames"
    async with session_scope() as session:
        row = await session.get(UserPreferenceRow, logged_in.subject_id)
    assert row is not None
    assert row.video_mode is None


async def test_invalid_video_mode_is_rejected_without_touching_timezone(
    logged_in: TestEnv,
) -> None:
    """非法模式 400，且不能顺手把同一次请求里的时区先写进去。"""
    await logged_in.client.put(
        "/v1/settings/preferences", json={"timezone": "Asia/Tokyo"}
    )
    response = await logged_in.client.put(
        "/v1/settings/preferences",
        # 顺序无关：路由里两个字段各自校验；这里断言"坏的那个整体不生效"。
        json={"timezone": "America/New_York", "video_mode": "ultra"},
    )
    assert response.status_code == 400, response.text
    after = (await logged_in.client.get("/v1/settings/preferences")).json()
    assert after["timezone"] == "Asia/Tokyo", "坏请求把同一请求里的时区写进去了"
    assert after["video_mode"] == "frames"


async def test_preferences_are_per_subject(logged_in: TestEnv) -> None:
    """偏好按 subject 存：别人的时区不能串到我的「今天」上。"""
    async with session_scope() as session:
        session.add(
            UserPreferenceRow(subject_id="subj_someone_else", timezone="Asia/Tokyo")
        )
    body = (await logged_in.client.get("/v1/settings/preferences")).json()
    assert body["timezone"] == "Asia/Shanghai", "读到了别人的偏好"
    assert body["stored"] is None


async def test_timezone_by_subject_skips_bad_and_empty(logged_in: TestEnv) -> None:  # noqa: ARG001
    """批量映射只带**有有效偏好**的 subject；坏值交给实例级默认兜底。

    主动投递按它给每个人算"今天"。把坏值传下去会让 `ZoneInfo` 在投递时抛异常，
    而把没有偏好的人也塞进来则会让所有人共用一个人的时区。
    """
    async with session_scope() as session:
        session.add(UserPreferenceRow(subject_id="subj_good", timezone="Asia/Tokyo"))
        session.add(UserPreferenceRow(subject_id="subj_bad", timezone="UTC+9"))
        session.add(UserPreferenceRow(subject_id="subj_empty", timezone=None))
    async with session_scope() as session:
        mapping = await timezone_by_subject(session)
    assert mapping.get("subj_good") == "Asia/Tokyo"
    assert "subj_bad" not in mapping
    assert "subj_empty" not in mapping
