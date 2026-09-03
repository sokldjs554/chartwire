import base64

import pytest
from pydantic import ValidationError

from chartwire.core.config import Settings


def test_env_prefix_and_test_isolation_fields(monkeypatch):
    monkeypatch.setenv("CHARTWIRE_TEST_DB", "chartwire_test_z")
    monkeypatch.setenv("CHARTWIRE_TEST_REDIS_DB", "9")
    monkeypatch.setenv("CHARTWIRE_CHUNK_MS", "100")
    s = Settings()
    assert (s.test_db, s.test_redis_db, s.chunk_ms) == ("chartwire_test_z", 9, 100)


def test_kek_master_must_be_32_bytes(monkeypatch):
    monkeypatch.setenv("CHARTWIRE_KEK_MASTER", base64.b64encode(b"short").decode())
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("CHARTWIRE_KEK_MASTER", base64.b64encode(bytes(range(32))).decode())
    assert Settings().kek_master_bytes == bytes(range(32))


def test_dev_defaults_are_flagged(monkeypatch):
    monkeypatch.delenv("CHARTWIRE_KEK_MASTER", raising=False)
    monkeypatch.delenv("CHARTWIRE_JWT_SECRET", raising=False)
    assert Settings().uses_dev_secrets is True
    monkeypatch.setenv("CHARTWIRE_KEK_MASTER", base64.b64encode(bytes(range(32))).decode())
    monkeypatch.setenv("CHARTWIRE_JWT_SECRET", "x" * 32)
    assert Settings().uses_dev_secrets is False


def test_note_provider_is_validated(monkeypatch):
    monkeypatch.setenv("CHARTWIRE_NOTE_PROVIDER", "gpt")
    with pytest.raises(ValidationError):
        Settings()
