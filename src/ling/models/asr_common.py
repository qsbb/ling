"""文件语音识别适配器共用的校验与 HTTP 失败分类。"""

from __future__ import annotations

import math

import httpx

from ling.ports.speech import AsrCapabilities, AsrRequest, SpeechError, SpeechErrorKind


def check_request(request: AsrRequest, capabilities: AsrCapabilities) -> None:
    if not request.model_id.strip():
        raise SpeechError(SpeechErrorKind.invalid_request, "请选择语音识别模型")
    if request.task != "transcribe":
        raise SpeechError(SpeechErrorKind.unsupported, "当前识别接口支持原语言转写")
    if request.language and not capabilities.language_hints:
        raise SpeechError(SpeechErrorKind.unsupported, "当前识别接口未提供语言提示参数")
    if request.vocabulary and not capabilities.vocabulary:
        raise SpeechError(SpeechErrorKind.unsupported, "当前识别接口未提供热词参数")
    if not request.audio.content:
        raise SpeechError(SpeechErrorKind.invalid_audio, "语音文件为空")
    if request.audio.spec.container not in capabilities.formats:
        raise SpeechError(SpeechErrorKind.invalid_audio, "请将语音转换为识别器支持的格式")
    if capabilities.max_file_bytes and len(request.audio.content) > capabilities.max_file_bytes:
        raise SpeechError(SpeechErrorKind.too_large, "语音文件超过识别接口的大小限制")


def response_body(response: httpx.Response) -> dict:
    """仅返回结构化响应，错误消息不携带供应商正文或音频。"""
    status = response.status_code
    if status in (401, 403):
        raise SpeechError(SpeechErrorKind.authentication, "语音识别鉴权失败，请检查密钥和权限")
    if status == 429:
        raise SpeechError(SpeechErrorKind.rate_limited, "语音识别服务限流，请稍后重试", retriable=True)
    if status in (400, 404, 405, 413, 415, 422):
        raise SpeechError(SpeechErrorKind.invalid_request, "语音识别请求被拒绝，请检查模型和音频格式")
    if status >= 500:
        raise SpeechError(SpeechErrorKind.unavailable, "语音识别服务暂时不可用", retriable=True)
    if status != 200:
        raise SpeechError(SpeechErrorKind.protocol, f"语音识别接口返回异常状态（{status}）")
    try:
        body = response.json()
    except ValueError as exc:
        raise SpeechError(SpeechErrorKind.protocol, "语音识别响应无法解析") from exc
    if not isinstance(body, dict):
        raise SpeechError(SpeechErrorKind.protocol, "语音识别响应结构不符合协议")
    if body.get("code") not in (None, "", 0, "0"):
        raise SpeechError(SpeechErrorKind.protocol, "语音识别服务返回业务错误")
    return body


def result_text(value: object) -> str:
    if not isinstance(value, str):
        raise SpeechError(SpeechErrorKind.protocol, "语音识别响应缺少文本")
    text = value.strip()
    if not text:
        raise SpeechError(SpeechErrorKind.unintelligible, "这段语音没有识别出可用文字")
    return text


def optional_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise SpeechError(SpeechErrorKind.protocol, "语音识别用量字段不符合协议")
    if not math.isfinite(value) or value < 0:
        raise SpeechError(SpeechErrorKind.protocol, "语音识别用量字段不符合协议")
    return float(value)


def optional_tokens(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SpeechError(SpeechErrorKind.protocol, "语音识别 token 用量字段不符合协议")
    return value
