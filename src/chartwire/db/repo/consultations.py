"""``consultation_requests`` (write-only from the api: the demo homepage form)."""

from __future__ import annotations

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.db.models import ConsultationRequest


async def create_request(
    session: AsyncSession,
    *,
    clinic_name: str,
    contact_name: str,
    phone: str,
    email: str,
    role: str | None,
    message: str | None,
    source: str,
) -> ConsultationRequest:
    stmt = (
        insert(ConsultationRequest)
        .values(
            clinic_name=clinic_name,
            contact_name=contact_name,
            phone=phone,
            email=email,
            role=role,
            message=message,
            source=source,
        )
        .returning(ConsultationRequest)
    )
    return (await session.scalars(stmt)).one()
