#!/usr/bin/env bash
# 로컬 개발 부트스트랩 (spec §0 세션 시작 체크리스트): PG/Redis 기동 → 역할 생성 → 마이그레이션 → 데모 시드.
# 멱등: 몇 번을 실행해도 같은 상태에 도달한다. `make dev-up` 이 이 스크립트를 호출한다.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then set -a; . ./.env; set +a; elif [ -f .env.example ]; then set -a; . ./.env.example; set +a; fi
: "${CHARTWIRE_SUPERUSER_URL:=postgresql://postgres@/postgres}"
export CHARTWIRE_SUPERUSER_URL

log() { printf '\033[1;34m[dev_up]\033[0m %s\n' "$*"; }

# --- PostgreSQL -----------------------------------------------------------------
if ! pg_isready -q 2>/dev/null; then
  log "PostgreSQL 기동"
  if command -v pg_ctlcluster >/dev/null; then sudo pg_ctlcluster 16 main start || true
  elif command -v brew >/dev/null; then brew services start postgresql@16 || true
  fi
  for _ in $(seq 1 20); do pg_isready -q 2>/dev/null && break; sleep 0.5; done
fi
pg_isready -q || { echo "PostgreSQL 에 연결할 수 없습니다" >&2; exit 1; }

# --- Redis ------------------------------------------------------------------------
if ! redis-cli ping >/dev/null 2>&1; then
  log "Redis 기동"
  redis-server --daemonize yes >/dev/null
  for _ in $(seq 1 20); do redis-cli ping >/dev/null 2>&1 && break; sleep 0.25; done
fi
redis-cli ping >/dev/null || { echo "Redis 에 연결할 수 없습니다" >&2; exit 1; }

# --- roles / schema / seed --------------------------------------------------------------
log "역할·데이터베이스 생성 (멱등)"
chartwire db bootstrap-roles
log "마이그레이션 적용"
chartwire db upgrade
if chartwire seed --help >/dev/null 2>&1; then
  log "데모 시드 (합성 데이터, 비어 있을 때만)"
  chartwire seed --demo --if-empty
else
  log "seed 명령이 아직 없어 시드를 건너뜁니다 (WP-F)"
fi
log "완료 — 다음: make demo (api + 콘솔) 또는 make test-unit"
