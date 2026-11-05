"""需要真实平台或设备凭据的用例（默认不执行）。

这些用例写成可执行的 pytest 函数并标记 ``w1_qq`` / ``w2_module``，
默认被 pytest 的 marker 过滤排除——它们依赖真实 QQ 账号、真实模型凭据或
真实设备，无法在 CI 里跑。需要时用 ``-m w1_qq`` 或 ``-m w2_module`` 显式启动。

每个用例都写清：测什么不变量、需要什么外部前提、失败时应看到什么。
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.w1_qq, pytest.mark.w2_module]


# ------------------------------------------------------------------ W1 QQ


@pytest.mark.w1_qq
def test_qq_signature_rejects_without_public_key() -> None:
    """未配置 webhook 公钥时必须拒绝一切入站事件（默认关闭，不宽松通过）。"""
    pytest.skip("W3 待测：需要 QQ webhook 公钥")


@pytest.mark.w1_qq
def test_qq_duplicate_event_is_ignored() -> None:
    """同一 (connector_instance, external_event_id) 只处理一次。"""
    pytest.skip("W3 待测：需要模拟平台回调")


@pytest.mark.w1_qq
def test_qq_private_and_group_windows_are_isolated() -> None:
    """私聊内容不得出现在群聊窗口；群聊窗口 visibility 必须是 group_scope。"""
    pytest.skip("W3 待测：需要 QQ 私聊与群聊权限")


@pytest.mark.w1_qq
def test_qq_proactive_requires_probe() -> None:
    """没有探测通过的主动能力时，主动推送必须被硬门拒绝。"""
    pytest.skip("W3 待测：需要账号权限探测")


@pytest.mark.w1_qq
def test_qq_uncertain_delivery_is_reconciled_not_resent() -> None:
    """5xx 或缺少 message_id 时标 uncertain，走对账而不是重发。"""
    pytest.skip("W3 待测：需要可控的失败注入平台")


@pytest.mark.w1_qq
def test_qq_unbound_account_goes_to_pending_not_guessed() -> None:
    """未绑定的 QQ 账号不得被猜成某个 subject。"""
    pytest.skip("W3 待测：需要两个 QQ 测试账号")


# ------------------------------------------------------------------ W2 记忆


@pytest.mark.w2_module
def test_memory_recall_filters_permission_before_ranking() -> None:
    """先过滤权限/有效期/撤回，再排序：群聊上下文不得召回私聊记忆。"""
    pytest.skip("W3 待测")


@pytest.mark.w2_module
def test_memory_retract_invalidates_derived_content() -> None:
    """撤回后立即不再被召回；派生摘要与胶囊同时失效。"""
    pytest.skip("W3 待测")


@pytest.mark.w2_module
def test_memory_model_inference_not_promoted_to_fact() -> None:
    """origin=model_inference 的候选不得进入可检索长期记忆。"""
    pytest.skip("W3 待测")


# ------------------------------------------------------------------ W2 关系/情绪


@pytest.mark.w2_module
def test_relationship_ignores_model_inference() -> None:
    """模型推测不得改写关系投影。"""
    pytest.skip("W3 待测")


@pytest.mark.w2_module
def test_affect_decays_and_never_grants_permission() -> None:
    """情绪强度随时间衰减；情绪不能放宽任何权限。"""
    pytest.skip("W3 待测")


# ------------------------------------------------------------------ W2 生活/目标


@pytest.mark.w2_module
def test_activity_cannot_complete_without_evidence() -> None:
    """没有完成证据不能标 completed；缺食材不能说做完。"""
    pytest.skip("W3 待测")


@pytest.mark.w2_module
def test_activity_pause_and_resume_from_checkpoint() -> None:
    """打断后可暂停并从检查点恢复。"""
    pytest.skip("W3 待测")


@pytest.mark.w2_module
def test_commitment_reschedule_invalidates_old_timer() -> None:
    """改期后旧定时任务失效，只有一条 active 承诺。"""
    pytest.skip("W3 待测")


@pytest.mark.w2_module
def test_commitment_undeliverable_is_not_marked_sent() -> None:
    """平台不支持主动投递时标 undeliverable，不得记成已发送。"""
    pytest.skip("W3 待测")


# ------------------------------------------------------------------ W2 主动性


@pytest.mark.w2_module
def test_proactive_gate_blocks_quiet_hours_and_limits() -> None:
    """勿扰时段、每日限额、渠道不可达都必须拦下。"""
    pytest.skip("W3 待测")


# ------------------------------------------------------------------ W2 跨端


@pytest.mark.w2_module
def test_handoff_capsule_expires_after_30_minutes() -> None:
    """交接胶囊默认 30 分钟后不可读。"""
    pytest.skip("W3 待测")


@pytest.mark.w2_module
def test_private_to_group_requires_per_turn_consent() -> None:
    """私聊内容进入群聊需要当次明确同意，且只对当前输出有效。"""
    pytest.skip("W3 待测")


@pytest.mark.w2_module
def test_service_restart_restores_commitment_and_capsule_expiry() -> None:
    """服务重启后承诺、胶囊剩余有效期、已执行动作都正确恢复。"""
    pytest.skip("W3 待测")


# ------------------------------------------------------------------ W2 语音/具身


@pytest.mark.w2_module
def test_asr_partial_never_enters_main_chain() -> None:
    """partial 只做字幕；只有 final 成为正式输入。"""
    pytest.skip("W3 待测：需要语音设备")


@pytest.mark.w2_module
def test_interruption_discards_late_audio() -> None:
    """插话取消旧轮；迟到音频带旧 turn_id 被丢弃。"""
    pytest.skip("W3 待测：需要语音设备")


@pytest.mark.w2_module
def test_playback_receipt_required_before_claiming_spoken() -> None:
    """没有播放回执不能声称已经说给用户听。"""
    pytest.skip("W3 待测")


@pytest.mark.w2_module
def test_perception_defaults_off_and_expires() -> None:
    """相机/位置/空间感知默认关闭；授权过期后在场证据失效。"""
    pytest.skip("W3 待测：需要手机设备")


# ------------------------------------------------------------------ W2 手机/微信


@pytest.mark.w2_module
def test_flutter_client_uses_same_auth_and_session_contract() -> None:
    """手机端与 Web 使用同一认证与 Session 契约。"""
    pytest.skip("W3 待测：需要真机或模拟器")


@pytest.mark.w2_module
def test_wechat_account_type_capabilities_are_separate() -> None:
    """公众号/小程序客服/微信客服各自独立能力矩阵，不互相假设。"""
    pytest.skip("W3 待测：需要确定微信账号类型与资质")


# ------------------------------------------------------------------ W2 安全负例


@pytest.mark.w2_module
def test_cross_audience_leak_is_zero() -> None:
    """硬门槛：跨受众私密泄漏必须为零。"""
    pytest.skip("W3 待测")


@pytest.mark.w2_module
def test_no_fabricated_send_or_execution() -> None:
    """硬门槛：不得虚构已发送或已执行。"""
    pytest.skip("W3 待测")


@pytest.mark.w2_module
def test_old_turn_cannot_overwrite_new_state() -> None:
    """硬门槛：旧轮次回写必须为零（revision 栅栏）。"""
    pytest.skip("W3 待测")
