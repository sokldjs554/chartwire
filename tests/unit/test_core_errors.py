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
