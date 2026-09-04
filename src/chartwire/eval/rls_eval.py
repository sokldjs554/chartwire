"""``rls.json`` — route × role × tenant cross-access eval (spec §11.1, §8.1, ADR-0001).

Two entry points, like :mod:`chartwire.eval.purge_eval`:

* :func:`run` **executes** the matrix against the real ``create_app()``: every registered route ×
  every role of tenant A (the ``rbac.MATRIX`` decision), the anonymous caller, and — the part the
  RBAC suite does not cover — every route instantiated with **tenant A's real resource ids** called
  with a **tenant B** token. A *leak* is any of those cross-tenant calls that does not fail: RLS
  scopes every statement by ``app.tenant_id`` and there is no runtime BYPASSRLS role (§0.7), so
  tenant B must get 403/404, never a 2xx carrying tenant A's row. The tally goes to
  ``var/eval/rls.json``.
* :func:`collect` validates that file and re-emits it under the common report header.

The route walk, the path instantiation and the app wiring are the ones
``tests/integration/test_rbac_matrix.py`` and ``tests/integration/api_support.py`` use — the same
decision table and the same fixtures (see :mod:`chartwire.eval.harness_env`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from chartwire.auth import rbac
from chartwire.core.config import Settings
from chartwire.eval.harness_env import EvalEnv, eval_env, fixtures, import_fixture
from chartwire.eval.purge_eval import collect as _collect
from chartwire.eval.purge_eval import write_measurement

DEFAULT_INPUT: Final = Path("var/eval/rls.json")
REQUIRED: Final[tuple[str, ...]] = ("attempts", "leaks")
TENANT_SLUGS: Final = ("rls-eval-a", "rls-eval-b")

_ID_SUBJECT: Final[dict[str, str]] = {
    "patients": "patient",
    "sessions": "session",
    "consents": "consent",
    "notes": "note",
    "alerts": "alert",
    "users": "user",
    "purge-jobs": "purge_job",
    "statements": "statement",
    "dead-letters": "dead_letter",
}


def collect(source: Path = DEFAULT_INPUT) -> dict[str, Any]:
    return _collect(source, required=REQUIRED)


# --------------------------------------------------------------------------- the executing run


@dataclass
class Tally:
    attempts: int = 0
    leaks: int = 0
    role_denied: int = 0
    role_allowed: int = 0
    anonymous_rejected: int = 0
    cross_tenant_attempts: int = 0
    violations: list[str] = field(default_factory=list)

    def leak(self, what: str) -> None:
        self.leaks += 1
        if len(self.violations) < 50:
            self.violations.append(what)


def _rbac_helpers() -> tuple[Any, Any]:
    """``api_routes`` / ``concrete_path`` from the RBAC matrix suite (one route walk, not two)."""
    module = import_fixture("tests.integration.test_rbac_matrix")
    return module.api_routes, module.concrete_path


def _headers(settings: Settings, tenant_id: UUID, role: str, user_id: UUID) -> dict[str, str]:
    headers = import_fixture("tests.integration.api_support").headers
    out: dict[str, str] = dict(headers(settings, tenant_id=tenant_id, role=role, user_id=user_id))
    return out


def _subject_of(template: str, name: str) -> str:
    """Which real id a path parameter wants, inferred from the collection segment before it."""
    prefix = template.split("{" + name + "}")[0].rstrip("/").rsplit("/", 1)[-1]
    return _ID_SUBJECT.get(prefix, prefix)


def _replace_param(template: str, path: str, name: str, value: str) -> str:
    """Replace the segment of ``path`` that ``{name}`` occupies in ``template``."""
    parts_t, parts_p = template.strip("/").split("/"), path.strip("/").split("/")
    if len(parts_t) != len(parts_p):
        return path
    for i, part in enumerate(parts_t):
        if part == "{" + name + "}":
            parts_p[i] = value
    return "/" + "/".join(parts_p)


def _concrete(route: Any, substitutions: dict[str, str] | None = None) -> tuple[str, int]:
    """``(path, how many parameters were pinned to a real tenant-A id)``."""
    _, concrete_path = _rbac_helpers()
    path: str = concrete_path(route)
    pinned = 0
    for name in route.param_convertors if substitutions else ():
        value = (substitutions or {}).get(_subject_of(route.path, name))
        if value is not None:
            path = _replace_param(route.path, path, name, value)
            pinned += 1
    return path, pinned


async def _tenant_a_subjects(env: EvalEnv, api_support: Any, settings: Settings) -> dict[str, str]:
    """Real tenant-A ids to aim the cross-tenant calls at (a session with segments, note and alert)."""
    tenant = env.tenant
    clinician = await env.factories.user(tenant.id, "clinician")
    patient = await env.factories.patient(tenant.id)
    await api_support.real_patient_dek(
        env.app_engine, tenant, patient.id, env.deps.kek, name="가상환자 RLS", phone="010-0000-0000"
    )
    consent_id = await api_support.grant(env.app_engine, tenant.id, patient.id)
    session = await env.factories.session(tenant.id, patient.id, clinician.id)
    seeded = await api_support.seed_session(
        env.app_engine,
        env.owner_engine,
        env.redis,
        env.objectstore,
        settings,
        tenant=tenant,
        patient_id=patient.id,
        clinician_id=clinician.id,
        session=session,
    )
    subjects = {
        "patient": str(patient.id),
        "session": str(session.id),
        "consent": str(consent_id),
        "user": str(clinician.id),
    }
    if seeded.signed_note_id is not None:
        subjects["note"] = str(seeded.signed_note_id)
    if seeded.risk_event_ids:
        subjects["alert"] = str(seeded.risk_event_ids[0])
    return subjects


async def _role_pass(
    api: Any,
    settings: Settings,
    tenant_id: UUID,
    route: Any,
    method: str,
    allowed: set[str],
    users: dict[str, Any],
    body: dict[str, Any] | None,
    tally: Tally,
) -> None:
    """Tenant A's own roles on a random-id path: the ``rbac.MATRIX`` decision must hold (§8.1)."""
    path, _ = _concrete(route)
    for role, user in users.items():
        response = await api.request(
            method, path, json=body, headers=_headers(settings, tenant_id, role, user.id)
        )
        tally.attempts += 1
        if not allowed or role in allowed:
            tally.role_allowed += 1
            if response.status_code in (401, 403):
                tally.leak(f"{method} {route.path} {role} -> {response.status_code} (허용 역할인데 거부)")
        elif response.status_code == 403:
            tally.role_denied += 1
        else:
            tally.leak(f"{method} {route.path} {role} -> {response.status_code} (거부돼야 함)")
    anonymous = await api.request(method, path, json=body)
    tally.attempts += 1
    if allowed:
        if anonymous.status_code == 401:
            tally.anonymous_rejected += 1
        else:
            tally.leak(f"{method} {route.path} anonymous -> {anonymous.status_code} (401 이어야 함)")


async def _tenant_pass(
    api: Any,
    settings: Settings,
    tenant_b_id: UUID,
    route: Any,
    method: str,
    allowed: set[str],
    users: dict[str, Any],
    subjects: dict[str, str],
    body: dict[str, Any] | None,
    tally: Tally,
) -> None:
    """Tenant B calling tenant A's real ids: RLS must hide the row — a 2xx is a leak."""
    if not allowed:
        return  # public routes carry no tenant-scoped row
    path, pinned = _concrete(route, subjects)
    if not pinned:
        return  # no id of a real tenant-A row in this path — nothing cross-tenant to attempt
    for role in sorted(allowed):
        response = await api.request(
            method, path, json=body, headers=_headers(settings, tenant_b_id, role, users[role].id)
        )
        tally.attempts += 1
        tally.cross_tenant_attempts += 1
        if response.status_code < 400:
            tally.leak(f"{method} {route.path} tenant-B/{role} -> {response.status_code} (교차 테넌트 노출)")


async def run(
    settings: Settings | None = None, *, out: Path = DEFAULT_INPUT, migrate: bool = True
) -> dict[str, Any]:
    """Execute the route × role × tenant matrix and write ``out`` (``var/eval/rls.json``)."""
    settings = settings or Settings()
    started = time.monotonic()
    api_support, _ = fixtures()
    api_routes, _concrete_path = _rbac_helpers()
    tally = Tally()
    async with eval_env(settings, migrate=migrate) as env:
        env.tenant = await env.factories.tenant(TENANT_SLUGS[0])
        tenant_b = await env.factories.tenant(TENANT_SLUGS[1])
        await api_support.real_record_key(env.owner_engine, tenant_b, env.deps.kek)
        subjects = await _tenant_a_subjects(env, api_support, settings)
        users_a = {r: await env.factories.user(env.tenant.id, r) for r in sorted(rbac.USER_ROLES)}
        users_b = {r: await env.factories.user(tenant_b.id, r) for r in sorted(rbac.USER_ROLES)}
        app = api_support.build_app(env.deps)
        routes = api_routes(app)
        async with api_support.client(app) as api:
            for route in routes:
                for method in sorted(route.methods - {"HEAD"}):
                    allowed = rbac.MATRIX[rbac.route_key(method, route.path)]
                    body: dict[str, Any] | None = {} if method in ("POST", "PUT") else None
                    await _role_pass(
                        api, settings, env.tenant.id, route, method, allowed, users_a, body, tally
                    )
                    await _tenant_pass(
                        api, settings, tenant_b.id, route, method, allowed, users_b, subjects, body, tally
                    )
        report: dict[str, Any] = {
            "attempts": tally.attempts,
            "leaks": tally.leaks,
            "routes": len({rbac.route_key(m, r.path) for r in routes for m in r.methods if m != "HEAD"}),
            "roles": len(rbac.USER_ROLES),
            "cross_tenant_attempts": tally.cross_tenant_attempts,
            "role_denied": tally.role_denied,
            "role_allowed": tally.role_allowed,
            "anonymous_rejected": tally.anonymous_rejected,
            "violations": tally.violations,
            "duration_s": round(time.monotonic() - started, 2),
            "db": env.db_name,
            "generated_at": datetime.now(tz=UTC).isoformat(),
        }
    write_measurement(out, report)
    return report
