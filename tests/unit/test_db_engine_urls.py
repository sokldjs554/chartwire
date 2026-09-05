"""Driver normalization for connection strings handed out by managed hosts.

Render (``fromDatabase.connectionString``) and Heroku-style add-ons give a plain ``postgresql://`` or the
legacy ``postgres://`` URL with no driver; the engine factories must put ``asyncpg``/``psycopg`` on either
so the same string can serve as both ``CHARTWIRE_DATABASE_URL`` and ``CHARTWIRE_DATABASE_OWNER_URL``
(render.yaml binds both to one connection string).
"""

from __future__ import annotations

import pytest

from chartwire.db.engine import as_async_url, as_sync_url


@pytest.mark.parametrize("scheme", ["postgresql", "postgres", "postgresql+psycopg", "postgresql+asyncpg"])
def test_every_postgres_scheme_gets_the_requested_driver(scheme: str) -> None:
    url = f"{scheme}://chartwire:secret@dpg-abc123-a/chartwire_db"
    a, s = as_async_url(url), as_sync_url(url)
    assert a.drivername == "postgresql+asyncpg" and s.drivername == "postgresql+psycopg"
    assert (a.username, a.password, a.host, a.database) == (
        "chartwire",
        "secret",
        "dpg-abc123-a",
        "chartwire_db",
    )
    assert s.render_as_string(hide_password=False) == a.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )


@pytest.mark.parametrize("url", ["mysql://u:p@h/db", "sqlite:///x.db", "redis://h:6379/0"])
def test_non_postgres_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="not a PostgreSQL URL"):
        as_async_url(url)
