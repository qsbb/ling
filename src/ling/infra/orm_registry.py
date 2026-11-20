"""Register every table on the shared SQLAlchemy metadata before schema operations."""


def import_all_models() -> None:
    from ling.infra import (  # noqa: F401
        asr_jobs,
        conversation_policy_audit,
        media_assets,
        media_playback,
        models,
        proactive_quota,
    )
