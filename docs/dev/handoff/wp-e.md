# WP-E 핸드오프 — Phase 0: auth / crypto / consent gates

담당 범위(Phase 0): `src/chartwire/auth/`, `src/chartwire/crypto/`, `src/chartwire/consent/{scopes,gates}.py`,
`tests/unit/test_auth_*.py`, `tests/unit/test_crypto_*.py`, `tests/unit/test_consent_gates.py`, `docs/security/threat-model.md`.
Phase 1 항목(`purge/`, `audit/`, `api/`, `worker/handlers/purge_*.py`, `consent/service.py`, `docs/consent-purge.md`)은 아직 시작하지 않았습니다.

## 만든 것 (모듈 맵)

| 모듈 | 내용 | 스펙 |
|---|---|---|
| `auth/jwt.py` | HS256 JWT를 `hmac`/`base64` 만으로 구현. `issue(claims, ttl, *, secret, clock)`, `verify(token, *, secret, clock) -> Principal`. 헤더를 `{"alg":"HS256","typ":"JWT"}` 로 고정하고 다른 `alg`(none/RS256/…)는 서명 검사 전에 `alg_mismatch` 로 거부. 서명은 `hmac.compare_digest`. `Principal(sub, tenant_id, role, jti, exp)` frozen dataclass + `user_id` 프로퍼티(sub 가 UUID 가 아니면 None). `TokenError(reason)` — reason 은 `malformed|alg_mismatch|bad_signature|missing_claim|expired|unknown_role`. `ROLES` = 5개 사용자 역할 + `service`. | §8.1 |
| `auth/rbac.py` | `MATRIX: dict[str, set[str]]` — §6.9 의 37개 라우트 전부(`"METHOD /v1/path"`, 헬스 3개는 `/healthz`,`/readyz`,`/metrics`). 빈 집합 = 공개 라우트(`PUBLIC`), `ANY` = 5개 사용자 역할. `check(principal|None, method, path_template) -> bool` (매트릭스에 없는 라우트는 `KeyError` — fail-closed 이면서 소리 나게), `is_public`, `require(*roles)` FastAPI 의존성 팩토리(403 `CW-4030`), `USER_ROLES`. | §8.1, §6.9 |
| `auth/deps.py` | `current_principal(authorization=Header, secret=Depends(jwt_secret), clk=Depends(clock))`. `jwt_secret()` 는 기본으로 `CHARTWIRE_JWT_SECRET` 환경변수를 읽고 `app.dependency_overrides` 로 교체 가능. 401 은 `WWW-Authenticate: Bearer` + `{"code":"CW-4010","reason":…}`; 토큰을 절대 응답에 되돌리지 않음. `parse_bearer` 분리. | §8.1 |
| `auth/passwords.py` | `hash(password) -> "scrypt$n$r$p$salt$hash"`, `verify(password, stored) -> bool` (파라미터가 해시와 함께 저장, 상수시간 비교, 손상된 문자열은 예외 없이 False). n=2^14, r=8, p=1, 16 B salt, 32 B key. | §8.1 |
| `auth/cli.py` | typer 앱 `app` (`chartwire token issue …`). `--tenant-id <uuid>` 면 DB 없이 발급, `--tenant <slug>` 면 `CHARTWIRE_DATABASE_URL` 로 psycopg 동기 조회(`SELECT id FROM tenants WHERE slug=%s`; tenants 는 RLS 없음/app SELECT 가능). `--user` 생략 시 `sub="dev:<role>"`. `--ttl` 분. 토큰은 stdout 에만. `resolve_tenant(..., resolver=)` 로 테스트에서 DB 를 대체. | §8.1, §13.1 |
| `crypto/envelope.py` | `Envelope.new_dek/encrypt/decrypt` (AES-256-GCM, `nonce(12)||ct||tag(16)`), `aad(tenant_id, scope, scope_id, field)`, `dek_fingerprint(wrapped)=sha256`. 이전 시도에서 이미 완성돼 있었고 검토 후 그대로 둠. | §3.1, §8.2 |
| `crypto/kek.py` | `KekProvider` Protocol, `LocalKek` (KEK=HKDF-SHA256(master, info=kek_ref); wrap=AES-GCM(kek, dek, aad=kek_ref)), `from_base64/from_env`, `hkdf_sha256`, `master_from_base64`, `AwsKmsKek` (boto3 lazy import, 클라이언트 주입 가능). | §3.1, §8.2 |
| `crypto/blind_index.py` | `blind_index(master, tenant_id, value)` = HMAC-SHA256(HKDF(master, `bidx:{tenant}`), NFKC→strip→lower(value)); `normalize`, `blind_index_key`. | §3.1 |
| `crypto/keycache.py` | `KeyCache(kek, maxsize=1000, ttl_s=600, clock=)` — `get(scope_id, kek_ref, wrapped|None)`, `invalidate(scope_id)`, `clear`, `stats`. 래핑 DEK 지문을 함께 저장해 재래핑 후 stale 키를 주지 않음; `wrapped=None` 은 `DekDestroyedError` + 캐시 제거. | §3.1, §8.2 |
| `crypto/errors.py` | `CryptoError > DecryptError(reason) / DekDestroyedError(scope_id) / KekUnavailableError`. 메시지에 키·평문 없음. | §0.9 |
| `consent/scopes.py` | `Scope` Literal, `SCOPES` frozenset, `SCOPE_ORDER`, 상수 4개. | §8.3 |
| `consent/gates.py` | 순수 함수 `active_scopes(consent_rows) -> set[str]`, `has_scope`, `require_scope(scopes, scope)` → `ConsentScopeMissing(scope)` (`code="CW-4031"`, `ws_code=4011`, `status=403`, `retryable=False`, 한국어 `detail`). `ConsentRow` Protocol(`version`, `scopes`, `revoked_at`) 이라 SQLAlchemy 모델을 그대로 넘길 수 있음. | §8.3 |
| `docs/security/threat-model.md` | STRIDE 12행 + 추가 3항목, 행마다 완화 파일/테스트 경로, 키 계층·회전·KMS 범위 밖 단락. | §8.6 |

LOC: 구현 984줄(기존 crypto 445줄 포함), 테스트 990줄.

## 테스트 실행

```bash
source /home/user/.venvs/proj/bin/activate
python -m pytest tests/unit/test_auth_*.py tests/unit/test_crypto_*.py tests/unit/test_consent_gates.py -q
ruff check src/chartwire/auth src/chartwire/consent src/chartwire/crypto tests/unit/test_auth_*.py tests/unit/test_crypto_*.py tests/unit/test_consent_gates.py
mypy --strict src/chartwire/auth src/chartwire/consent src/chartwire/crypto
```

DB/Redis 불필요(`CHARTWIRE_TEST_DB=chartwire_test_e`, Redis index 5 는 사용하지 않았음). hypothesis: 봉투 round-trip 300, AAD 불일치 200, 비트 플립 200, 블라인드 인덱스 200 예제.
결과(최종): 279 passed, 0 failed.

## 스펙과 다르게 한 점 (이유)

1. **`jwt.issue/verify` 가 `secret=` 키워드 인자를 요구** — Phase 0 규칙상 `chartwire.core.config` 를 import 할 수 없어 값을 주입받습니다. `deps.jwt_secret` 의존성이 env 를 읽고, Phase 1 에서 `Settings.jwt_secret` 으로 오버라이드할 예정입니다. `clock` 도 같은 이유로 구조적 Protocol(`now()`)을 로컬에 두었고 `core.clock.Clock` 과 호환됩니다.
2. **`require_scope(scopes, scope)` 시그니처** — 태스크 지시대로 순수 함수(스코프 집합 입력). 스펙의 `require_scope(session_or_patient, scope)` 는 Phase 1 `consent/service.py` 가 행을 로드한 뒤 이 함수를 호출하는 래퍼로 제공합니다.
3. **`active_scopes` 의 "최신 비철회 행" 해석** — version 이 가장 큰 행이 지배하고, 그 행이 철회됐으면 빈 집합. 이전 버전이 되살아나는 fail-open 해석은 채택하지 않음(테스트 `test_revoked_latest_version_yields_nothing_even_if_older_row_unrevoked`).
4. **`AwsKmsKek` 는 KMS `Encrypt/Decrypt` 사용** (스펙 문구는 `GenerateDataKey/Decrypt`). `Envelope.new_dek()` 가 로컬에서 DEK 를 만들고 `wrap(dek)` 인터페이스가 이미 있는 키를 래핑하므로 `Encrypt` 가 정확한 매핑입니다. `kek_ref` 를 KeyId 와 EncryptionContext 양쪽에 바인딩.
5. **헬스 라우트 경로**: `/healthz`, `/readyz`, `/metrics` 는 `/v1` 접두사 없이 등록(운영 프로브 관례). `api/app.py`(Phase 1, WP-E) 가 같은 경로로 마운트해야 RBAC 매트릭스 테스트가 통과합니다.
6. **에러 코드**: 401 은 `CW-4010`, 역할 거부 403 은 `CW-4030`(스펙이 정한 `CW-4031` 옆자리). 지금은 `HTTPException(detail={"code",…})` 형태이고, Phase 1 에서 `core.errors.AppError` → problem+json 으로 통일합니다.
7. **`Principal.exp` 필드 추가** — 스펙 4필드 외에 만료 시각을 실어 WS hello 에서 티켓 TTL 과 비교할 수 있게 함.
8. **dev 토큰의 `sub`** — `--user` 생략 시 `dev:<role>` 문자열(UUID 아님). `Principal.user_id` 는 None 이 되고 `TenantCtx.user_id: UUID | None` 과 맞습니다.

## 다른 WP 에 요청

- **WP-A (`src/chartwire/cli.py`)**: 아래 두 줄을 추가해 주세요.
  ```python
  from chartwire.auth.cli import app as token_app
  app.add_typer(token_app, name="token")   # → chartwire token issue --tenant demo --role clinician
  ```
- **WP-A (`core/errors.py`)**: Phase 1 에서 `ConsentScopeMissing` 을 problem+json 으로 매핑할 핸들러를 `api/app.py`(WP-E) 에 둘 예정이므로 변경 요청 없음. 다만 `AppError` 시그니처가 §3.1 대로(`code, status, detail, retryable`)이면 그대로 어댑터를 붙입니다.
- **WP-B (`ws/ingest.py` hello 처리)**: 동의 게이트는 `chartwire.consent.gates.active_scopes(rows)` + `require_scope(scopes, "recording")` 를 쓰고, `ConsentScopeMissing.ws_code`(=4011) 로 닫아 주세요. 티켓 검증 뒤 JWT 는 다시 보지 않아도 됩니다(티켓 payload 에 role/user 포함).
- **WP-C (`stt/worker.py`)**: 청크마다 `require_scope(scopes, "transcription")`, final 삽입 시 `has_scope(scopes, "search_index")` 로 `segment_search` 행 여부 결정.
- **WP-D (`worker/handlers/note_draft.py`)**: `require_scope(scopes, "ai_drafting")` 의 `ConsentScopeMissing` 을 잡아 `abstained: consent_scope_missing` 으로 기록.
- **WP-G (`outbox.HandlerContext`)**: `kek: KekProvider`, `keycache: KeyCache` 필드는 이 패키지의 타입을 그대로 쓰면 됩니다. `keys:invalidate` 구독자는 payload(sid|pid 문자열)를 `keycache.invalidate(scope_id)` 에 넘기면 됩니다.

## 알려진 이슈 / 남은 일 (Phase 1)

- `tests/integration/test_rbac_matrix.py`(라우트 × 역할 200/403)는 `api/app.py` 가 생긴 뒤 작성.
- `chartwire token issue --tenant <slug>` 의 DB 조회 경로는 단위 테스트에서 resolver 주입으로만 검증했고 실제 psycopg 호출은 WP-A 의 `tenants` 테이블이 올라온 뒤 통합 테스트에서 확인 예정.
- `KeyCache` 는 스레드 안전하지 않음(단일 asyncio 루프 프로세스에서 사용하는 전제; 문서화됨).
