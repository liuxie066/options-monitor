from __future__ import annotations

from src.infrastructure.futu_gateway import (
    FutuGatewayAuthExpiredError, FutuGatewayNeed2FAError,
    FutuGatewayNeedPicVerifyError, FutuGatewayRateLimitError,
)
from src.infrastructure.opend_retcodes import OpenDRetCode, classify_opend_error
from src.infrastructure.opend_watchdog import classify_watchdog_result


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
        ("需要图形验证码", OpenDRetCode.NEED_PIC_VERIFY),
        ("image captcha required", OpenDRetCode.NEED_PIC_VERIFY),
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


def test_auth_reason_codes_match_watchdog_and_gateway() -> None:
    cases = (
        ("登录密码被修改,已退出登录", FutuGatewayAuthExpiredError, "OPEND_LOGIN_INVALID"),
        ("需要手机验证码", FutuGatewayNeed2FAError, "OPEND_NEEDS_PHONE_VERIFY"),
        ("需要图形验证码", FutuGatewayNeedPicVerifyError, "OPEND_NEEDS_PIC_VERIFY"),
    )
    for message, error_type, reason_code in cases:
        assert classify_watchdog_result(None, message)[0] == reason_code
        assert classify_opend_error(message).reason_code == reason_code
        assert error_type(message).reason_code == reason_code
