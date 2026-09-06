import pytest

from chartwire.core.errors import AppError, Conflict, Forbidden, NotFound


def test_problem_document_is_rfc9457_shaped():
    problem = AppError("CW-4031", 403, "동의 범위 없음").to_problem(request_id="r-1")
    assert problem == {
        "type": "urn:chartwire:error:CW-4031",
        "title": "Forbidden",
        "status": 403,
        "detail": "동의 범위 없음",
        "code": "CW-4031",
        "request_id": "r-1",
        "retryable": False,
    }


def test_retryable_server_error():
    err = AppError("CW-5503", 503, "redis unavailable", retryable=True)
    assert err.retryable and err.to_problem()["title"] == "Service Unavailable"
    assert "CW-5503" in repr(err)


@pytest.mark.parametrize("code", ["CW-1234", "cw-4001", "CW-40010", "E-4001"])
def test_invalid_codes_rejected(code):
    with pytest.raises(ValueError):
        AppError(code, 400, "x")


def test_convenience_subclasses():
    assert NotFound("세션").status == 404 and "세션" in NotFound("세션").detail
    assert Forbidden().status == 403 and Conflict("dup").status == 409


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        ("노트", "노트를 찾을 수 없습니다"),  # 받침 없음
        ("세션", "세션을 찾을 수 없습니다"),  # 받침 ㄴ
        ("문장", "문장을 찾을 수 없습니다"),  # 받침 ㅇ
        ("환자", "환자를 찾을 수 없습니다"),
        ("파기 작업", "파기 작업을 찾을 수 없습니다"),
    ],
)
def test_not_found_picks_the_korean_object_particle(resource, expected):
    """detail 은 콘솔이 사용자에게 그대로 보여 주는 문장이라 ``노트을(를)`` 로 두지 않는다."""
    assert NotFound(resource).detail == expected


@pytest.mark.parametrize("resource", ["note", "01a0757b", ""])
def test_non_hangul_resources_keep_the_ambiguous_particle(resource):
    """영문·숫자·빈 문자열은 읽는 법이 갈리므로 판정하지 않는다."""
    assert NotFound(resource).detail == f"{resource}을(를) 찾을 수 없습니다"
