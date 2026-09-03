"""scrypt password hashing: format, salt uniqueness, verification, malformed input never raises."""

from __future__ import annotations

import pytest

from chartwire.auth import passwords


def test_hash_format_and_verify() -> None:
    stored = passwords.hash("가상비밀번호-1234")
    scheme, n, r, p, salt, digest = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", "16384", "8", "1")
    assert salt and digest
    assert passwords.verify("가상비밀번호-1234", stored)
    assert not passwords.verify("가상비밀번호-1235", stored)


def test_same_password_hashes_differently_per_salt() -> None:
    a, b = passwords.hash("x"), passwords.hash("x")
    assert a != b
    assert passwords.verify("x", a) and passwords.verify("x", b)


def test_parameters_travel_with_hash() -> None:
    stored = passwords.hash("pw").replace("$16384$", "$4096$", 1)
    assert not passwords.verify("pw", stored)  # digest was computed with n=16384


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "scrypt",
        "bcrypt$1$2$3$4$5",
        "scrypt$x$8$1$AAAA$AAAA",
        "scrypt$16384$8$1$not-base64!$AAAA",
        "a$b$c$d$e$f$g",
    ],
)
def test_malformed_stored_value_verifies_false(stored: str) -> None:
    assert passwords.verify("pw", stored) is False


def test_empty_password_refused() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        passwords.hash("")
