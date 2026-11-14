"""人格删除必须保留仍被会话或平台实例选中的设置。"""

from __future__ import annotations

import pytest

from ling.domains.conversation.service import ConversationService
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import WindowDialogueOverride
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web
ORIGIN = {"Origin": "http://testserver"}


@pytest.mark.parametrize("scope", ["onebot", "qqbot"])
async def test_persona_used_by_instance_cannot_be_deleted(
    logged_in: TestEnv, scope: str
) -> None:
    persona = await logged_in.client.post(
        "/v1/personas", json={"display_name": "实例人格", "system_prompt": "测试"}
    )
    assert persona.status_code == 201, persona.text
    persona_id = persona.json()["persona_id"]
    instance = await logged_in.client.post(
        f"/v1/settings/connectors/{scope}/instances",
        headers=ORIGIN,
        json={"instance_id": "sample", "display_name": "测试实例", "enabled": True},
    )
    assert instance.status_code == 201, instance.text
    url = f"/v1/settings/connectors/{scope}/instances/sample/dialogue-policy"
    change = {"set": {"persona_id": persona_id}, "clear": []}
    preview = await logged_in.client.post(f"{url}/preview", json=change, headers=ORIGIN)
    assert preview.status_code == 200, preview.text
    saved = await logged_in.client.put(
        url,
        json={**change, "preview_token": preview.json()["preview_token"]},
        headers=ORIGIN,
    )
    assert saved.status_code == 200, saved.text

    blocked = await logged_in.client.delete(f"/v1/personas/{persona_id}")
    assert blocked.status_code == 409, blocked.text
    assert "persona_instance_override:1" in blocked.text

    clear = {"set": {}, "clear": ["persona_id"]}
    preview = await logged_in.client.post(f"{url}/preview", json=clear, headers=ORIGIN)
    assert preview.status_code == 200, preview.text
    cleared = await logged_in.client.put(
        url,
        json={**clear, "preview_token": preview.json()["preview_token"]},
        headers=ORIGIN,
    )
    assert cleared.status_code == 200, cleared.text
    deleted = await logged_in.client.delete(f"/v1/personas/{persona_id}")
    assert deleted.status_code == 204, deleted.text


async def test_persona_delete_only_reports_visible_session_override_count(
    logged_in: TestEnv,
) -> None:
    persona = await logged_in.client.post(
        "/v1/personas", json={"display_name": "会话人格", "system_prompt": "测试"}
    )
    assert persona.status_code == 201, persona.text
    persona_id = persona.json()["persona_id"]
    own = await logged_in.client.post("/v1/sessions", json={"title": "本人"})
    assert own.status_code == 201, own.text
    async with session_scope() as session:
        foreign = await IdentityService(session).create_user(
            username="foreign-member", password="member-password", role="member"
        )
        foreign_window = await ConversationService(
            session, actor_id=logged_in.settings.actor_id
        ).create_window(subject_id=foreign.subject_id)
        for window_id in (own.json()["session_id"], foreign_window.id):
            session.add(
                WindowDialogueOverride(
                    window_id=window_id,
                    revision=1,
                    overrides={"persona_id": persona_id},
                    updated_by="test-user",
                )
            )

    blocked = await logged_in.client.delete(f"/v1/personas/{persona_id}")
    assert blocked.status_code == 409, blocked.text
    assert "persona_session_override:1" in blocked.text
    assert "persona_session_override:2" not in blocked.text
