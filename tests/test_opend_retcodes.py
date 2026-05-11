from __future__ import annotations

from src.infrastructure.futu_gateway import FutuGatewayRateLimitError
from src.infrastructure.opend_retcodes import OpenDRetCode, classify_opend_error


def test_classify_opend_error_prefers_exception_code() -> None:
    assert classify_opend_error(FutuGatewayRateLimitError("anything")) is OpenDRetCode.RATE_LIMIT


def test_classify_opend_error_covers_all_hint_categories() -> None:
    cases = [
        ("rate limit", OpenDRetCode.RATE_LIMIT),
        ("too frequent", OpenDRetCode.RATE_LIMIT),
        ("频率太高", OpenDRetCode.RATE_LIMIT),
        ("最多10次", OpenDRetCode.RATE_LIMIT),
        ("频率限制", OpenDRetCode.RATE_LIMIT),
        ("请求过快", OpenDRetCode.RATE_LIMIT),
        ("login expired", OpenDRetCode.AUTH_EXPIRED),
        ("auth expired", OpenDRetCode.AUTH_EXPIRED),
        ("token expired", OpenDRetCode.AUTH_EXPIRED),
        ("not logged", OpenDRetCode.AUTH_EXPIRED),
        ("not login", OpenDRetCode.AUTH_EXPIRED),
        ("2fa", OpenDRetCode.NEED_2FA),
        ("phone verification", OpenDRetCode.NEED_2FA),
        ("verify code", OpenDRetCode.NEED_2FA),
        ("手机验证码", OpenDRetCode.NEED_2FA),
        ("短信验证", OpenDRetCode.NEED_2FA),
        ("手机验证", OpenDRetCode.NEED_2FA),
        ("验证码", OpenDRetCode.NEED_2FA),
        ("timeout", OpenDRetCode.TRANSIENT),
        ("disconnected", OpenDRetCode.TRANSIENT),
        ("connection reset", OpenDRetCode.TRANSIENT),
        ("broken pipe", OpenDRetCode.TRANSIENT),
        ("temporarily unavailable", OpenDRetCode.TRANSIENT),
        ("empty_chain", OpenDRetCode.EMPTY_CHAIN),
        ("empty", OpenDRetCode.EMPTY_CHAIN),
    ]

    for raw, expected in cases:
        assert classify_opend_error(raw) is expected


def test_classify_opend_error_dict_prefers_error_code_over_message() -> None:
    payload = {"error_code": "RATE_LIMIT", "message": "login expired"}
    assert classify_opend_error(payload) is OpenDRetCode.RATE_LIMIT


def test_classify_opend_error_unknown_inputs() -> None:
    assert classify_opend_error(None) is OpenDRetCode.UNKNOWN
    assert classify_opend_error("") is OpenDRetCode.UNKNOWN
    assert classify_opend_error({}) is OpenDRetCode.UNKNOWN
