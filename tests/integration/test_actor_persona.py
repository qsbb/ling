

async def test_renaming_persona_invalidates_name_cache(logged_in) -> None:
    """改名后必须失效名字缓存。

    缓存用于"剥掉她自己加的名字前缀"。不失效的话，改名后仍按旧名判断，
    新名字的前缀就漏过去了——而且这种错只在改名后才出现，很难联想到缓存。
    """
    from ling.capabilities import naming

    await naming.display_name_for("persona_ling")
    before = dict(naming._NAME_CACHE)  # noqa: SLF001 - 就是要看缓存
    assert before, "缓存没建立"

    await logged_in.client.put(
        "/v1/persona",
        json={"display_name": "新名字", "system_prompt": "你是新名字"},
    )
    assert naming._NAME_CACHE == {}, "改名后缓存没有失效"  # noqa: SLF001

    assert await naming.display_name_for("persona_ling") == "新名字"
