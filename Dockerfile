# syntax=docker/dockerfile:1.7
# 다단계 빌드 (spec §12.2): 1단계에서 wheel 을 만들고, 2단계는 비-root + tini 런타임만 담는다.
FROM python:3.11-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip wheel \
 && pip wheel --no-cache-dir --wheel-dir /wheels ".[aws]"

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    CHARTWIRE_OBJECTSTORE=localfs:/var/lib/chartwire/objects
# /var/lib/chartwire/scripts is a named volume in docker-compose. The image must create and own the
# mount point here: Docker copies the existing directory's ownership up into a fresh named volume, but
# an absent path is created root:root — and the container runs as uid 10001, so `chartwire seed --demo`
# would fail with PermissionError writing the demo scripts, and every service that waits on `migrate`
# would never start.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 chartwire \
 && useradd --system --uid 10001 --gid chartwire --home-dir /app --no-create-home chartwire \
 && mkdir -p /app /var/lib/chartwire/objects /var/lib/chartwire/scripts \
 && chown -R chartwire:chartwire /app /var/lib/chartwire
COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links=/wheels chartwire[aws] && rm -rf /wheels
WORKDIR /app
COPY --chown=chartwire:chartwire console ./console
USER chartwire
EXPOSE 8000
# /healthz 는 liveness 전용(드레인 중에도 200) — 컨테이너가 드레인 중인 프로세스를 죽이지 않게 한다.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]
ENTRYPOINT ["tini", "--"]
# 셸 형식이어야 ${CHARTWIRE_ROLE} 이 확장된다; exec 로 PID 를 넘겨 SIGTERM 이 그대로 도달한다 (§6.4 drain, §7.3).
CMD ["sh", "-c", "exec chartwire serve ${CHARTWIRE_ROLE:-api}"]
