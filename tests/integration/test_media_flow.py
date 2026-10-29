

def test_upload_note_does_not_point_at_a_nonexistent_field() -> None:
    """上传接口的提示不能指路到不存在的字段。

    原来写的是"把 evidence_id 放进 media_evidence_ids"，而 `SendTurnBody`
    里没有这个字段——照着做什么也不会发生，用户只会以为图片功能坏了。

    只检查 **note 那段文案本身**：源码里的注释会提到这个字段名（解释为什么
    删掉它），拿整段源码做断言会误伤。
    """
    import pathlib
    import re

    from ling.api.routes import chat

    body_fields = set(chat.SendTurnBody.model_fields)
    source = pathlib.Path("src/ling/api/routes/chat.py").read_text()
    notes = re.findall(r'"note":\s*"([^"]*)"', source)
    offending = [n for n in notes if "media_evidence_ids" in n]
    assert not offending or "media_evidence_ids" in body_fields, offending
