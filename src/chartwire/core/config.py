"""Process settings (env prefix ``CHARTWIRE_``).

Every runtime process (api, worker, stt-worker) and every CLI command reads one
``Settings`` instance. The values below are development defaults that match
``.env.example``; production deployments override them through the environment.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_KEK_MASTER = base64.b64encode(bytes(32)).decode()


class Settings(BaseSettings):
    """Runtime configuration. Field names map 1:1 to ``CHARTWIRE_<NAME>``."""

    model_config = SettingsConfigDict(env_prefix="CHARTWIRE_", extra="ignore")

    # --- PostgreSQL ------------------------------------------------------
    database_url: str = "postgresql+asyncpg://chartwire_app:chartwire_app@localhost:5432/chartwire"
    """Runtime role ``chartwire_app`` (NOBYPASSRLS). The only URL runtime processes use."""
    database_owner_url: str = "postgresql+psycopg://chartwire_owner:chartwire_owner@localhost:5432/chartwire"
    """``chartwire_owner``: migrations, schema dump, fixtures. Never used by runtime processes."""
    superuser_url: str | None = "postgresql://app:app@localhost:5432/postgres"
    """Superuser: ``db bootstrap-roles``, the perf study and the superuser-leak test only."""
    db_single_role: bool = False
    """Managed-PG fallback (§4.1): app and owner are the same role; FORCE RLS keeps isolation."""
    owner_password: str = "chartwire_owner"
    app_password: str = "chartwire_app"

    # --- Redis / storage ---------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    objectstore: str = "localfs:./var/objects"
    """``localfs:<dir>`` or ``s3://<bucket>``."""

    # --- secrets -----------------------------------------------------------
    kek_master: str = _DEV_KEK_MASTER
    """Base64 of 32 bytes. ``chartwire dev keygen`` prints a fresh one."""
    jwt_secret: str = "dev-secret-change-me"

    # --- AI adapter --------------------------------------------------------
    note_provider: Literal["extractive", "anthropic"] = "extractive"
    anthropic_model: str | None = None

    # --- ingest protocol ---------------------------------------------------
    allow_sim_frames: bool = True
    chunk_ms: int = 200
    credit_base: int = 50
    ledger_flush_ms: int = 50
    ledger_flush_rows: int = 500
    stream_maxlen: int = 2000
    session_idle_timeout_s: int = 3600

    # --- process identity --------------------------------------------------
    node_id: str = "dev-1"
    scripts_dir: Path | None = None
    """Directory with the Redis Lua scripts; ``None`` = packaged ``chartwire/redis/scripts``."""
    embedded: bool = False
    """``chartwire serve all --embedded``: api + worker + stt-worker in one process."""

    # --- test isolation (tests/conftest.py) --------------------------------
    test_db: str = "chartwire_test"
    test_redis_db: int = 1

    @field_validator("kek_master")
    @classmethod
    def _kek_is_32_bytes(cls, value: str) -> str:
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError as exc:  # pragma: no cover - message is what matters
            raise ValueError("kek_master must be base64") from exc
        if len(raw) != 32:
            raise ValueError("kek_master must decode to exactly 32 bytes")
        return value

    @property
    def kek_master_bytes(self) -> bytes:
        return base64.b64decode(self.kek_master)

    @property
    def uses_dev_secrets(self) -> bool:
        """True when a dev default secret is still in use (health endpoint warns)."""
        return self.kek_master == _DEV_KEK_MASTER or self.jwt_secret == "dev-secret-change-me"


def get_settings() -> Settings:
    """Build settings from the environment (no caching: tests mutate the environment)."""
    return Settings()
