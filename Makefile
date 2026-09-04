# chartwire — 개발/검증/측정 진입점 (spec §12.2). 모든 숫자는 코드가 만든 JSON 에서만 README 로 들어간다 (§11.4).
# 사용법: `make dev-up` → `make test-unit` → `make test-integration`. 측정 타깃(loadtest-*, perf-study, bulk)은
# 다른 작업이 없는 유휴 상태에서 직렬로만 실행한다 (§0 time-box).
.DEFAULT_GOAL := help
SHELL := /bin/bash

PY ?= python
CHARTWIRE ?= chartwire
PYTEST ?= pytest -p no:cacheprovider
SEED ?= 42
BULK_SEED ?= 7
BULK_SEGMENTS ?= 2000000
LOAD_SESSIONS ?= 50
LOAD_DURATION ?= 60
DEMO_PORT ?= 8000
STRICT_MODULES := src/chartwire/ws/core.py src/chartwire/ws/codec.py src/chartwire/notes/verifier.py src/chartwire/crypto src/chartwire/outbox
# 테스트 격리: 작업 패키지마다 자기 DB/Redis 인덱스를 쓴다 (docs/dev/AGENT_ENV.md)
export CHARTWIRE_TEST_DB ?= chartwire_test
export CHARTWIRE_TEST_REDIS_DB ?= 1

.PHONY: help dev-up test test-unit test-integration lint migrate-roundtrip schema-dump schema-check eval \
	loadtest-a loadtest-b loadtest-c loadtest-d loadtest-h perf-study bulk cdk-synth readme-numbers demo

help: ## 타깃 목록
	@grep -E '^[a-zA-Z_ %-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

dev-up: ## PG/Redis 기동 → 역할 생성 → 마이그레이션 → 데모 시드 (멱등)
	./scripts/dev_up.sh

test: test-unit test-integration ## 전체 테스트

test-unit: ## 단위 테스트 (서비스 불필요, hypothesis 포함)
	$(PYTEST) tests/unit -q

test-integration: ## PG + Redis 가 필요한 스위트를 직렬로 실행 (xdist 금지)
	$(PYTEST) tests/integration tests/ws tests/rls tests/chaos -q -p no:xdist

lint: ## ruff + ruff format --check + mypy(strict 모듈) + pip-audit
	ruff check src tests scripts
	ruff format --check src tests scripts
	mypy $(wildcard $(STRICT_MODULES))
	pip-audit --progress-spinner off || true

migrate-roundtrip: ## upgrade head → downgrade base → upgrade head, 그리고 스키마 덤프 비교
	$(CHARTWIRE) db upgrade head
	$(CHARTWIRE) db downgrade base
	$(CHARTWIRE) db upgrade head
	$(CHARTWIRE) db schema-dump --check

schema-dump: ## docs/db/schema.sql 재생성 (pg_dump --schema-only --no-owner, 변동 라인 제거)
	$(CHARTWIRE) db schema-dump --out docs/db/schema.sql

schema-check: ## 커밋된 schema.sql 과 라이브 스키마 비교
	$(CHARTWIRE) db schema-dump --check

eval: ## 평가 전체 실행 → docs/eval/*.json (§11.1)
	$(CHARTWIRE) eval all --seed $(SEED) --out docs/eval

loadtest-a loadtest-b loadtest-c loadtest-d loadtest-h: loadtest-%: ## 부하 시나리오 A|B|C|D|H → docs/loadtest/*.json (§11.2)
	$(CHARTWIRE) loadtest $(shell echo $* | tr a-z A-Z) --sessions $(LOAD_SESSIONS) --duration $(LOAD_DURATION) --out docs/loadtest

perf-study: ## 실행계획 연구 (0006 before → 0007 after) → docs/perf (§4.6); bulk 이후 VACUUM ANALYZE 필수
	$(CHARTWIRE) db downgrade 0006 && $(CHARTWIRE) perf study --state before --out docs/perf
	$(CHARTWIRE) db upgrade head && $(CHARTWIRE) perf study --state after --out docs/perf

bulk: ## 합성 대량 적재 (8 테넌트 / 2M 세그먼트, §4.6) → docs/perf/bulk.json
	$(CHARTWIRE) synth bulk --seed $(BULK_SEED) --segments $(BULK_SEGMENTS) --out docs/perf/bulk.json

cdk-synth: ## AWS CDK 합성 + cdk-nag, 커밋된 템플릿과 diff 0 (§12.1)
	cd infra/cdk && npx aws-cdk@2 synth --quiet && git diff --exit-code -- cdk.out

readme-numbers: ## README 숫자 마커를 docs/{eval,loadtest,perf}/*.json 에서 채운다 (§11.4)
	$(CHARTWIRE) readme-numbers --write

demo: ## api + worker + stt-worker 를 한 프로세스로 (http://localhost:8000/console) — 시드된 데모, 합성 데이터만
	$(CHARTWIRE) db bootstrap-roles && $(CHARTWIRE) db upgrade && $(CHARTWIRE) seed --demo --if-empty
	@echo "콘솔: http://127.0.0.1:8000/console  (clinician@demo.clinic / demo1234!)  — 흐름: docs/dev/e2e.md"
	$(CHARTWIRE) serve all --embedded --host 127.0.0.1 --port $(DEMO_PORT)
