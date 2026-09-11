"""``ConsultationIn`` (데모 홈페이지 상담신청 본문): 동의 없이는 거절, 전화번호 정규화, 알 수 없는 키 거절."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chartwire.api.schemas import ConsultationIn, ConsultationOut
from chartwire.core.errors import RateLimited

GOOD = {
    "clinic_name": "가상의원",
    "contact_name": "가상원장-001",
    "phone": "010-0000-0000",
    "email": "lead@example.test",
    "agree_privacy": True,
}


def test_minimal_body_parses_with_defaults():
    body = ConsultationIn.model_validate(GOOD)
    assert body.source == "console-home" and body.role is None and body.message is None
    assert body.agree_privacy is True


@pytest.mark.parametrize("value", [False, None, "no", 0])
def test_agree_privacy_must_be_true(value):
    with pytest.raises(ValidationError) as exc:
        ConsultationIn.model_validate({**GOOD, "agree_privacy": value})
    assert any(err["loc"] == ("agree_privacy",) for err in exc.value.errors())


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("010-0000-0000", "010-0000-0000"),
        ("  +82 10   0000  0000 ", "+82 10 0000 0000"),  # 공백 연속 → 하나, 양끝 제거
        ("0212345678", "0212345678"),
    ],
)
def test_phone_is_normalized(raw, normalized):
    assert ConsultationIn.model_validate({**GOOD, "phone": raw}).phone == normalized


@pytest.mark.parametrize(
    "bad",
    [
        "010.0000.0000",  # 허용 문자 밖(.)
        "010-0000-0000 ext 2",  # 문자
        "+ - -",  # 숫자 5개 미만
        "1234",  # 5자 미만
        "0" * 33,  # 32자 초과
        "010-0000-0000<script>",
    ],
)
def test_phone_rejects_other_characters(bad):
    with pytest.raises(ValidationError) as exc:
        ConsultationIn.model_validate({**GOOD, "phone": bad})
    assert any(err["loc"] == ("phone",) for err in exc.value.errors())


@pytest.mark.parametrize("bad", ["no-at-sign", "@domain", "local@", "a b@c.d", "ab"])
def test_email_needs_local_at_domain(bad):
    with pytest.raises(ValidationError):
        ConsultationIn.model_validate({**GOOD, "email": bad})


def test_email_is_stripped():
    assert (
        ConsultationIn.model_validate({**GOOD, "email": " lead@example.test "}).email == "lead@example.test"
    )


@pytest.mark.parametrize("field", ["clinic_name", "contact_name"])
def test_blank_names_reject(field):
    with pytest.raises(ValidationError):
        ConsultationIn.model_validate({**GOOD, field: "   "})
    assert getattr(ConsultationIn.model_validate({**GOOD, field: " 가상 "}), field) == "가상"


def test_role_is_an_enum_and_optional():
    assert ConsultationIn.model_validate({**GOOD, "role": "director"}).role == "director"
    with pytest.raises(ValidationError):
        ConsultationIn.model_validate({**GOOD, "role": "ceo"})


@pytest.mark.parametrize(
    ("field", "value"),
    [("clinic_name", "x" * 121), ("contact_name", "x" * 61), ("message", "x" * 2001), ("source", "x" * 65)],
)
def test_length_caps(field, value):
    with pytest.raises(ValidationError):
        ConsultationIn.model_validate({**GOOD, field: value})


@pytest.mark.parametrize("extra", ["ip", "user_agent", "tenant_id", "utm_source"])
def test_extra_fields_reject(extra):
    with pytest.raises(ValidationError) as exc:
        ConsultationIn.model_validate({**GOOD, extra: "x"})
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


def test_out_is_closed_too():
    with pytest.raises(ValidationError):
        ConsultationOut.model_validate(
            {"id": "0" * 32, "received_at": "2026-09-11T00:00:00Z", "message": "m", "phone": "x"}
        )


def test_rate_limited_error_carries_retry_after():
    err = RateLimited("too many", 17, code="CW-4291")
    assert err.status == 429 and err.retryable and err.headers == {"Retry-After": "17"}
    assert err.to_problem("r-1")["code"] == "CW-4291" and "Retry-After" not in err.to_problem("r-1")
    assert RateLimited("x", 0).headers == {"Retry-After": "1"}  # 윈도 경계에서도 0초는 내지 않는다


@pytest.mark.parametrize(
    "source",
    [
        "probe@example.test",  # 이메일을 로그 줄에 밀어 넣으려는 시도
        "010-1234-5678",  # 전화번호
        "console home",  # 공백
        "Console-Home",  # 대문자
        "콘솔홈",  # 한글
        "console_home",  # 밑줄
    ],
)
def test_source_is_a_closed_vocabulary(source):
    """``source`` 만 접수 로그에 실리므로 자유 문자열이면 안 된다 (``[a-z0-9-]``)."""
    with pytest.raises(ValidationError) as exc:
        ConsultationIn.model_validate({**GOOD, "source": source})
    assert exc.value.errors()[0]["type"] == "string_pattern_mismatch"


@pytest.mark.parametrize("source", ["console-home", "console-hero", "readme", "qr-2026"])
def test_source_accepts_slugs(source):
    assert ConsultationIn.model_validate({**GOOD, "source": source}).source == source
