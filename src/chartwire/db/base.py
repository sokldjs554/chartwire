"""Declarative base and column type aliases shared by all models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from sqlalchemy import BigInteger, MetaData, text
from sqlalchemy.dialects.postgresql import BYTEA, JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


TENANT_ISOLATION_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
"""The one RLS policy expression (spec §4.1): NULL context → 0 rows, fail-closed."""

uuid_pk = Annotated[UUID, mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))]
uuid_pk_client = Annotated[UUID, mapped_column(primary_key=True)]
tenant_fk = Annotated[UUID, mapped_column(nullable=False)]
bigint_identity = Annotated[int, mapped_column(BigInteger, primary_key=True, autoincrement=True)]
timestamptz = Annotated[datetime, mapped_column(TIMESTAMP(timezone=True))]
created_now = Annotated[
    datetime, mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))
]
bytea = Annotated[bytes, mapped_column(BYTEA)]
jsonb_obj = Annotated[dict, mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))]
jsonb_list = Annotated[list, mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))]
