# syntax=docker/dockerfile:1.7
# 공개 데모 전용 이미지 — PostgreSQL 16 · Redis 7 · api + worker + stt-worker 를 한 컨테이너에 담는다.
#
# 왜 이런 모양인가: Render 무료 플랜은 계정당 관리형 Postgres 1개와 Key Value 1개만 준다. 그 두 슬롯이
# 다른 프로젝트에 이미 쓰이고 있어 chartwire 가 붙을 관리형 DB 가 없다. 그래서 데모는 관리형 리소스를
# 하나도 쓰지 않고 무료 웹 서비스 한 개 안에서 자급자족한다.
#
# 이것은 데모 배포 형태이지 운영 배포 형태가 아니다. 운영에 가까운 형태는 저장소의 다른 두 곳에 있다:
#   · docker-compose.yml — postgres · redis · api · worker · stt-worker 를 분리한 컨테이너 5개
#   · infra/cdk/         — RDS PG16 · ElastiCache · ECS Fargate ×3 (synth 만, 미배포)
# 한 컨테이너에 데이터베이스를 함께 넣는 대가는 docs/limitations.md §8 에 적어 두었다.

FROM python:3.11-slim-bookworm AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip wheel \
 && pip wheel --no-cache-dir --wheel-dir /wheels "."

FROM python:3.11-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PATH="/usr/lib/postgresql/16/bin:${PATH}" \
    PGDATA=/var/lib/chartwire/pgdata \
    CHARTWIRE_RUN_DIR=/var/lib/chartwire/run \
    CHARTWIRE_OBJECTSTORE=localfs:/var/lib/chartwire/objects \
    CHARTWIRE_SCRIPTS_DIR=/var/lib/chartwire/scripts \
    CHARTWIRE_NOTE_PROVIDER=extractive \
    CHARTWIRE_ALLOW_SIM_FRAMES=true \
    CHARTWIRE_ROLE=all \
    CHARTWIRE_EMBEDDED=1 \
    CHARTWIRE_NODE_ID=render-demo-1

# PostgreSQL 16 은 bookworm 기본 저장소에 없어 PGDG 를 추가한다. contrib(pg_trgm·pgcrypto)는
# postgresql-16 패키지에 함께 들어 있다. curl/gnupg 는 저장소 등록에만 쓰고 지운다.
#
# 저장소 이름은 베이스 이미지의 배포판에서 읽는다(`VERSION_CODENAME`). 하드코딩하면 베이스가 다음
# Debian 으로 넘어갔을 때 libicu 버전이 어긋나 `postgresql-16` 이 설치되지 않는다 — 실제로 그렇게 깨졌다.
# 그래서 위에서 베이스 태그를 bookworm 으로 고정하고, 여기서는 그 태그를 따라가게 한다.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg tini redis-server \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16 \
 && apt-get purge -y curl gnupg \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 chartwire \
 && useradd --system --uid 10001 --gid chartwire --home-dir /app --no-create-home chartwire \
 && mkdir -p /app /var/lib/chartwire/objects /var/lib/chartwire/scripts \
             /var/lib/chartwire/pgdata /var/lib/chartwire/run \
 && chown -R chartwire:chartwire /app /var/lib/chartwire

COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links=/wheels chartwire && rm -rf /wheels

WORKDIR /app
COPY --chown=chartwire:chartwire console ./console
COPY --chown=chartwire:chartwire deploy/render-demo/start.sh /usr/local/bin/chartwire-demo-start
RUN chmod 0755 /usr/local/bin/chartwire-demo-start

USER chartwire
EXPOSE 8000
# /healthz 는 liveness 전용(드레인 중에도 200) — 컨테이너가 드레인 중인 프로세스를 죽이지 않게 한다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; p=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=3).status == 200 else 1)"]
ENTRYPOINT ["tini", "--"]
CMD ["chartwire-demo-start"]
