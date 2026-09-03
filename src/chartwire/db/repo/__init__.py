"""Repositories: the only place SQL is written. Every function takes the ``AsyncSession``
opened by ``db.tenant.tenant_tx`` and never commits itself."""
