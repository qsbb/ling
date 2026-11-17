"""入站去重与撤回认领以稳定实例键隔离。"""

from ling.kernel.inbound_intake import _connector_event_scope


def test_stable_instances_have_independent_event_scopes_for_shared_namespace() -> None:
    alpha = _connector_event_scope("onebot-shared", ("onebot", "alpha"))
    bravo = _connector_event_scope("onebot-shared", ("onebot", "bravo"))

    assert alpha == "connector:onebot:alpha"
    assert bravo == "connector:onebot:bravo"
    assert alpha != bravo
    assert _connector_event_scope("onebot-shared", None) == "connector:onebot-shared"
    assert _connector_event_scope("onebot-shared", ("onebot", "alpha"), recall=True) == (
        "connector:onebot:alpha:recall"
    )
