"""``chartwire perf study --out docs/perf [--only q1,q2] [--state before|after] [--runs 5]``."""

from __future__ import annotations

from pathlib import Path

import typer
from sqlalchemy.engine import make_url

from chartwire.core.config import get_settings
from chartwire.db.cli import libpq_url
from chartwire.db.engine import with_database
from chartwire.perf import queries as catalogue
from chartwire.perf import study as study_mod

app = typer.Typer(help="실행계획 연구 (§4.6) — superuser 연결, SET ROLE 전환", no_args_is_help=True)


@app.command("study")
def study_cmd(
    out: Path = typer.Option(
        Path("docs/perf"), "--out", help="plans/, leakproof.txt, summary.json 출력 디렉터리"
    ),
    only: str | None = typer.Option(None, "--only", help="예: q1,q2 (그룹) 또는 Q2b (개별)"),
    state: str | None = typer.Option(
        None, "--state", help="before(0006) | after(0007); 생략 시 alembic_version 으로 판정"
    ),
    runs: int = typer.Option(5, "--runs", min=1, help="워밍업 1회 뒤 측정 횟수 (중앙값)"),
    db_url: str | None = typer.Option(
        None, "--db-url", help="superuser URL (기본: CHARTWIRE_SUPERUSER_URL + 앱 DB 이름)"
    ),
    tenant: str | None = typer.Option(None, "--tenant", help="측정에 쓸 테넌트 slug (기본: 첫 bulk 테넌트)"),
    seed: int = typer.Option(7, "--seed", help="리포트 헤더의 시드 (bulk 시드)"),
) -> None:
    """현재 스키마 상태에서 Q1–Q6 를 측정하고 플랜·leakproof·summary 를 기록합니다."""
    settings = get_settings()
    if db_url is None:
        if not settings.superuser_url:
            raise typer.BadParameter("--db-url 또는 CHARTWIRE_SUPERUSER_URL 이 필요합니다")
        database = make_url(settings.database_url).database or "chartwire"
        db_url = with_database(settings.superuser_url, database).render_as_string(hide_password=False)
    if state is not None and state not in study_mod.STATES:
        raise typer.BadParameter("--state 는 before 또는 after")
    conn = study_mod.connect(libpq_url(db_url))
    try:
        resolved = state or study_mod.detect_state(study_mod.current_revision(conn))
        if resolved is None:
            raise typer.BadParameter("리비전을 판정할 수 없습니다 — --state 를 지정하세요")
        study = study_mod.run_study(
            conn, catalogue.select(only), state=resolved, runs=runs, tenant_slug=tenant, log=typer.echo
        )
    except study_mod.StudyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        conn.close()
    summary = study_mod.write_outputs(study, out, seed=seed)
    overhead = study.rls_overhead()
    typer.echo(f"summary → {summary} (rls overhead: {overhead})")
