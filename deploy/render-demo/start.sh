#!/bin/sh
# 공개 데모 컨테이너의 기동 스크립트 (deploy/render-demo/Dockerfile 의 CMD).
#
# PostgreSQL → Redis → 역할 → 마이그레이션 → 데모 시드 → api+worker+stt-worker 순으로 올린다.
# 모든 단계가 멱등이라 인스턴스가 잠들었다 깨어나 다시 실행돼도 안전하다: initdb 는 데이터 디렉터리가
# 비어 있을 때만, `db upgrade` 는 head 가 아닐 때만, `seed --demo --if-empty` 는 demo 테넌트가 없을 때만
# 실제로 일한다.
set -eu

RUNDIR=${CHARTWIRE_RUN_DIR:-/var/lib/chartwire/run}
PORT=${PORT:-8000}
STATE=$(dirname "$PGDATA")
# 포트는 컨테이너 안에서만 의미가 있지만 밖으로 뺀다: CI 와 로컬에서 이 스크립트를 그대로 돌려
# 검증할 수 있어야 하고, 그러려면 이미 떠 있는 PostgreSQL/Redis 와 겹치지 않아야 한다.
PGPORT=${PGPORT:-5432}
REDIS_PORT=${CHARTWIRE_DEMO_REDIS_PORT:-6379}
export PGPORT

mkdir -p "$RUNDIR" "$PGDATA"

rand() { python -c 'import secrets; print(secrets.token_urlsafe(24))'; }

# 컨테이너 밖으로 열리는 DB/Redis 포트가 없고(둘 다 127.0.0.1 만 듣는다) 무료 플랜의 디스크는
# 재배포마다 비워진다. 그래서 이미지에 비밀번호를 굽지 않고 기동할 때마다 새로 만든다.
SU_PW=$(rand)
CHARTWIRE_OWNER_PASSWORD=$(rand)
CHARTWIRE_APP_PASSWORD=$(rand)
export CHARTWIRE_OWNER_PASSWORD CHARTWIRE_APP_PASSWORD

# KEK 는 데이터베이스와 수명을 같이해야 한다: 키가 바뀌면 기존 세션 DEK 를 풀 수 없어 데모가
# 조용히 깨진다. 그래서 PGDATA 옆 파일에 보관하고, 환경변수로 들어온 값이 유효할 때만 그쪽을 쓴다
# (Render 의 generateValue 가 정확히 32 B 를 준다는 보장이 없다 — 형식을 직접 확인한다).
kek_valid() {
  python - "$1" <<'PY'
import base64, sys
try:
    sys.exit(0 if len(base64.b64decode(sys.argv[1], validate=True)) == 32 else 1)
except Exception:
    sys.exit(1)
PY
}

if [ -z "${CHARTWIRE_KEK_MASTER:-}" ] || ! kek_valid "$CHARTWIRE_KEK_MASTER"; then
  if [ -s "$STATE/kek_master" ]; then
    CHARTWIRE_KEK_MASTER=$(cat "$STATE/kek_master")
    echo "[start] KEK: 데이터 디렉터리 옆 파일에서 읽음"
  else
    CHARTWIRE_KEK_MASTER=$(python -c 'import os, base64; print(base64.b64encode(os.urandom(32)).decode())')
    ( umask 077 && printf '%s' "$CHARTWIRE_KEK_MASTER" > "$STATE/kek_master" )
    echo "[start] KEK: 새로 생성 (LocalKek 전용 — KMS 는 AWS 배포)"
  fi
fi
export CHARTWIRE_KEK_MASTER
: "${CHARTWIRE_JWT_SECRET:=$(rand)}"
export CHARTWIRE_JWT_SECRET

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "[start] initdb $PGDATA"
  initdb -D "$PGDATA" -U chartwire -E UTF8 --locale=C.UTF-8 \
         --auth-local=trust --auth-host=scram-sha-256 > /dev/null
fi

# fsync 와 synchronous_commit 은 켜 둔다. 이 프로젝트가 보여 주려는 것이 "ack 는 audio_chunks 가
# PostgreSQL 에 커밋된 뒤에만 나간다" 라서, 데모에서 그 보장을 끄면 보여 주는 대상이 달라진다.
# 무료 인스턴스(0.1 vCPU · 512 MB)에 맞춰 메모리 관련 값만 낮춘다.
echo "[start] postgres"
postgres -D "$PGDATA" \
  -c listen_addresses=127.0.0.1 \
  -c port="$PGPORT" \
  -c unix_socket_directories="$RUNDIR" \
  -c shared_buffers=32MB \
  -c max_connections=30 \
  -c work_mem=2MB \
  -c maintenance_work_mem=32MB \
  -c log_min_messages=warning &

i=0
until pg_isready -q -h "$RUNDIR" -p "$PGPORT" -U chartwire; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then echo "[start] postgres 가 60 초 안에 준비되지 않았습니다" >&2; exit 1; fi
  sleep 1
done

# 로컬 소켓은 trust 라 비밀번호 없이 superuser 로 붙는다. 이번 기동의 비밀번호를 stdin 으로 넘겨
# 프로세스 목록(argv)에 남지 않게 한다.
printf "ALTER ROLE chartwire PASSWORD '%s';\n" "$SU_PW" \
  | psql -q -h "$RUNDIR" -p "$PGPORT" -U chartwire -d postgres -v ON_ERROR_STOP=1 > /dev/null

export CHARTWIRE_SUPERUSER_URL="postgresql://chartwire:$SU_PW@127.0.0.1:$PGPORT/postgres"
export CHARTWIRE_DATABASE_URL="postgresql+asyncpg://chartwire_app:$CHARTWIRE_APP_PASSWORD@127.0.0.1:$PGPORT/chartwire"
export CHARTWIRE_DATABASE_OWNER_URL="postgresql+psycopg://chartwire_owner:$CHARTWIRE_OWNER_PASSWORD@127.0.0.1:$PGPORT/chartwire"

echo "[start] redis"
redis-server --save '' --appendonly no --bind 127.0.0.1 --port "$REDIS_PORT" \
  --maxmemory 64mb --maxmemory-policy noeviction --dir "$RUNDIR" &
export CHARTWIRE_REDIS_URL="redis://127.0.0.1:$REDIS_PORT/0"

# 이 컨테이너로 들어오는 길은 Render 의 프록시뿐이라(밖으로 열린 포트가 PORT 하나다) 그 프록시의
# X-Forwarded-For 를 믿어도 된다 — uvicorn 이 소켓 주소를 원 클라이언트로 복원해 주고, 상담 신청
# 레이트리밋(시간당 10건)이 그 주소로 갈린다. 직접 접근이 가능한 배포에서는 절대 켜면 안 된다:
# 아무나 헤더로 자기 버킷을 고를 수 있게 된다.
export CHARTWIRE_TRUSTED_PROXY_IPS="${CHARTWIRE_TRUSTED_PROXY_IPS:-*}"

i=0
until redis-cli -h 127.0.0.1 -p "$REDIS_PORT" ping > /dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 30 ]; then echo "[start] redis 가 30 초 안에 준비되지 않았습니다" >&2; exit 1; fi
  sleep 1
done

echo "[start] roles"
chartwire db bootstrap-roles -d chartwire
echo "[start] migrations"
chartwire db upgrade
echo "[start] demo seed (모든 데이터는 합성)"
chartwire seed --demo --if-empty

# 상담 신청 접수함 보존 90일 — 홈페이지의 개인정보 고지가 약속하는 파기를 기동 때마다 실제로 실행한다.
# 실패해도 기동은 계속한다(접수함이 비어 있거나 테이블이 아직 없을 수 있다).
chartwire db purge-consultations --older-than-days 90 || echo "[start] 접수함 파기 건너뜀"

echo "[start] serve all --embedded on 0.0.0.0:$PORT"
exec chartwire serve all --embedded --host 0.0.0.0 --port "$PORT"
