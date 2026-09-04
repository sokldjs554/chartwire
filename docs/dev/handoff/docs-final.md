# docs-final 핸드오프 — 숫자 조립 · README 정합 · 데모 자산 · 최종 게이트

> 상태: **완료** (2026-09-04). 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. git 상태를 바꾸는 명령은 실행하지 않았다.
> 실행 환경: `source /home/user/.venvs/proj/bin/activate; set -a; . ./.env.example; set +a` (개발 DB `chartwire`, Redis 0, 데모 포트 8000).
> 규칙 준수: 측정 숫자는 손으로 적지 않았다(전부 JSON → 마커). 라이선스 문구 없음. 부하 테스트는 실행하지 않았다(측정이 끝난 JSON 을 소비만 했다).

## 0. 체크리스트

- [x] 1. 숫자 조립 — `readme_numbers.py --write` (README) → `--write --readme docs/perf/README.md` → `chartwire loadtest results --out docs/loadtest` → `--check` **exit 0**
- [x] 2. README 정합 패스 — Q2 행 라벨 수정, RLS 오버헤드 서술을 측정값으로, N=200 무릎 명시, "왜 믿어도 되는가/안 되는가" 문단, held-out P/R 평문
- [x] 2. `docs/perf/README.md` §Q2 세 플랜 이야기(커밋된 플랜 파일 이름 인용)
- [x] 3. 데모 자산 — PNG 5장 갱신/신규, `docs/images/demo.gif`(40.8 s, 12 fps, 폭 1100, **7.1 MB**), README §3 임베드, 서버 SIGTERM 종료 확인
- [x] 4. 문서 — `limitations.md`, `AGENTS.md` 버그 저널(quality2 / loadfix / 데모 캡처), `dev/e2e.md` §5·§6, `eval/README.md` 해석 규칙 5–8, README §12 확인
- [x] 4. 링크 체크 — `scripts/check_links.py`(신규) → README + `docs/**/*.md` 45개 문서 상대 링크 **0 깨짐**
- [x] 5. 최종 게이트 — `make lint` / `pytest tests/unit` 827 / `--check` 0 / `sha256sum -c FROZEN.txt` OK / cdk 템플릿 diff 0

## 1. 숫자 조립 (지시 1)

```bash
python scripts/readme_numbers.py --write                                # 126개 마커, 미측정 행 1개(load.E) 삭제
python scripts/readme_numbers.py --write --readme docs/perf/README.md   # 56개 마커
chartwire loadtest results --out docs/loadtest                          # results.md 재렌더
python scripts/readme_numbers.py --check                                # exit 0
```

- **삭제된 행: `load.E`** — `docs/loadtest/E.json` 이 없다(시나리오 E = nginx 뒤 2 프로세스 drain, 스펙 §11.2 "시간이 남을 때만" · 컷 순서 맨 위).
  실행하지 않았고, README 표 ① 의 E 행은 파이프라인이 지웠다. `docs/limitations.md` §3 과 README §12 에 **미실행**으로 적었다.
- `Makefile` 의 `readme-numbers` 타깃을 **세 단계**로 넓혔다(README → `docs/perf/README.md` → `loadtest results` 재렌더). 이전에는 첫 단계뿐이라
  `make readme-numbers` 뒤에도 perf 문서와 `results.md` 가 뒤처졌다(WP-F 요청 6 · WP-H §4-4 가 손으로 두 줄 더 치라고 남긴 부분).
- `--check` 는 `docs/perf/README.md` 에 대해서도 exit 0 이다.

## 2. README 정합 패스 (지시 2)

절 번호가 하나씩 밀렸다: **§3 "5분 데모"를 신설**했고 그 뒤가 4–12 로 이동했다. 그 결과 스펙 §14 의 12번 항목(한계 · 채용공고 매핑 · 기존 저장소)이 **README §12** 다.

| 고친 것 | 전 | 후 |
|---|---|---|
| 표 ② Q2a | "부분 문자열 검색, RLS 아래 `LIKE`(트라이그램 인덱스 무시)" | "**2음절 `%불면%` as app** — 트라이그램(3-gram)이 만들어지지 않아 GIN 사용 불가" |
| 표 ② Q2b | "같은 검색, `SECURITY DEFINER search_segments()`" | "**3음절 `%불면증%` as app (RLS)** — GIN 이 있어도 미사용(`texticlike` 가 leakproof 아님)" |
| 표 ② Q2c | "`terms[]` 배열 인덱스(`@>`)" | "**같은 쿼리 as owner(RLS 면제)** — `ix_search_text_trgm` Bitmap Index Scan" |
| 표 ② Q2d | "함수 호출 wall time — text / term" | "**`search_segments()` wall time as app** — `('불면증','text')` / `('불면','term')`" |
| §5 테넌시 | "표 ② 의 **Q2a→Q2b** 가 그 차이" | "같은 SQL 의 세 실행: Q2b(app/RLS) → Q2c(owner) → Q2d(함수)"; 2음절 한계는 Q2a·Q2d_term 으로 |
| §5 RLS 비용 | (없음) | 측정 마커로 "Q1 은 십몇 %", "Q3 after 는 **음수** = 이 측정의 잡음 폭", "진짜 비용은 퍼센트가 아니라 플랜이 바뀌는 경우(Q2b)" |
| 표 ① A N=200 | "(1,000 chunk/s)" | "(목표 1,000 chunk/s — **무릎**)" + 표 아래 무릎 문단(실측 chunk/s·loss 0·credit 최솟값 마커) |
| §1 표 2행 | "**평평한 RSS** 를 시나리오 B 로 측정" | "유한 큐와 **api RSS 기울기**를 시나리오 B 로 측정"(측정값이 2.47 MB/min 이라 "평평"은 과장) |
| §8 위험 경보 | 표 ③ 참조만 | held-out **P/R/F1 평문**(마커) + "목표 미달", "재현율 1 이 아니다 = 경보가 없다고 위험이 없는 게 아니다", 누출 통제 한 줄 |
| §11 AI 개발 | 대표 결함 5개 | + 부하 측정이 잡은 3개(redis 풀 상한 100, 잔해가 다음 실행을 오염, `extra=` 소실) |
| §12 | 한계 요약 한 줄 | Anthropic tier · **시나리오 E** · N=200 무릎 · held-out 목표 미달을 명시 |
| §10 재현 명령 | 표 ③ = "RBAC/파기 스위트가 `var/eval/*.json` 을 씀" | `chartwire eval purge --run` · `rls --run`(quality2 가 실행형으로 바꿈) + 데모 자산 행 + 링크 체크 행 + 부하는 전용 DB |

**신설 문단 "왜 이 숫자를 믿어도 되는가 / 믿으면 안 되는가"**(§2 끝): 믿어도 되는 쪽 = JSON → 스크립트, 미측정 행 삭제, CI `frozen-artifacts` 의 `--check`,
리포트 헤더와 DB 대조. 믿으면 안 되는 쪽 = ① 같은 호스트 ② 클라이언트 혼입 ③ 합성 상한 ④ 실행 간 편차(C 의 변화율은 편차보다 작다) ⑤ held-out 은 안전망 지표.

`docs/perf/README.md` **§Q2 세 플랜 나란히**: 같은 SQL 이 역할에 따라 다른 플랜을 받는 것을 커밋된 플랜 파일(`plans/q2a_after.json`, `q2b_after.json`,
`q2c_after.json`)의 실제 노드로 서술했다 — Q2a/Q2b 는 `Bitmap Index Scan on ix_search_session` + `Filter: (text ~~* …)`, Q2c 는
`BitmapAnd(ix_search_text_trgm, ix_search_session)`. **Q2a 와 Q2b 가 같은 플랜인 이유가 서로 다르다**(2음절은 좁힐 것이 없어서, 3음절은 좁힐 수 있는데 RLS 가 막아서)를
읽는 법 1–4 로 적었고, `Q2d_term` 이 `Q2d_text` 보다 느린 것은 태거가 더 많은 행을 돌려주기 때문(플랜 결함 아님)이라고 밝혔다.

## 3. 데모 자산 (지시 3)

```bash
chartwire seed --demo --if-empty                       # 이미 시드됨 → 건너뜀
chartwire serve all --embedded --host 127.0.0.1 --port 8000 &
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
python scripts/console_screenshots.py --chromium /opt/pw-browsers/chromium \
       --patient 가상환자-0006 --video-dir <scratch>/video      # PNG 5장 + WebM 40.8 s
```

- `docs/images/01_recorder.png` · `02_live_alert.png` · `03_soap_draft.png` **갱신**, `04_purge_receipt.png` · `05_ops.png` **신규**, `demo.gif` **신규**.
  다섯 장 모두 상단의 빨간 **SYNTHETIC** 배너가 보이고, GIF 도 전 구간에서 보인다.
- 02 는 `harm_to_others severity 2` 배너 + SLA 카운트다운 + ACK + 발화별 e2e ms + `risk.alert … commit→view 4.0 ms`,
  04 는 `state=verified` 영수증 · `receipt_hash` · `unwrap=failed:dek_destroyed / decrypt_sample=failed:invalid_tag` 토스트,
  05 는 Ops 패널(메트릭 폴링 + DLQ 목록, admin).
- **드라이버(`scripts/console_screenshots.py`)를 세 군데 고쳤다**:
  1. **역할 전환** — 임상의로 철회한 뒤 `#purgeJobId` 를 읽고 `--admin`(기본 `admin@demo.clinic`)으로 **다시 로그인**해 `#trackPurge` → 영수증 → `verify-decrypt`.
     이유는 §5 의 발견(콘솔 403). 이전 실행은 여기서 60 s 타임아웃으로 죽었다(04·05 가 없던 이유).
  2. **캡처 전 `window.scrollTo(0,0)`** — 패널을 클릭하면 페이지가 가로로 밀려 04 의 왼쪽이 잘렸다.
  3. `--video-dir`(Playwright `record_video_dir`; WebM 은 컨텍스트를 닫아야 flush 된다) · `--no-shots` 추가.
- **GIF**: 40.84 s WebM(1440×1000) → `fps=12, scale=1100:-2:lanczos, palettegen(max_colors=48, stats_mode=diff)` + `paletteuse(dither=none, diff_mode=rectangle)`
  → **7,134,630 B (≤ 8 MB)**. 등속이며(배속 없음), 스크립트 재생 자체가 4배속이다. 프레임을 뽑아 한국어 가독성을 확인했다.
- 서버는 **SIGTERM 으로 내렸고 종료를 확인**했다: `drain started` → `ws drain started` → `stt-worker stopped {clean:true}` → `worker stopped {clean:true}` →
  `Application shutdown complete` → `Finished server process` → 포트 8000 닫힘. (`integrator.md` §7 이 미확인으로 남긴 항목이 닫혔다.)

## 4. 문서 (지시 4)

- `docs/limitations.md` — held-out **목표 미달**과 "재현율 1 이 아니다"의 의미, CI 임계값이 측정값에서 내려온 바닥이라는 것, fact_recall 이 §9.2 의 12문장 상한 때문에
  구조적으로 낮다는 것, **N=200 무릎**(스펙 기대 미달, 원인은 프로세스 CPU 천장, 수평 확장 미측정), **C 의 변화율 < 실행 간 편차**, 전용 부하 DB,
  `stt_offsets` 완결 판정의 "언제 읽었는가", **시나리오 E 미실행**, Q5 오버헤드의 부호(잡음), 콘솔 파기 영수증 403. Anthropic tier 미실행은 기존 문장을 유지했다.
- `docs/AGENTS.md` §6 버그 저널 — **품질 패스**(주입 정규식 갭 → leaks 4→0, cue 하나를 넣자 약물·음주가 무너진 12문장 상한 문제, `past`/`present_denial`, hypothesis 통계 파싱),
  **부하 재측정**(redis-py 풀 상한 100 = 하드 상한, `stt:active` 잔해 크래시 루프가 다른 시나리오의 숫자를 오염, `extra=` 로그 소실, 오염된 측정 DB,
  하네스가 불변식을 경주로 바꿔 놓은 것), **데모 캡처**(콘솔 403 역할 전환, 임베디드 stt-worker SIGTERM 종료 확인).
  통합자·WP-H 항목(stt-worker 좀비, `end` 500, `last_final_seq`, `final_e2e` 축)은 이미 있었고 그대로 뒀다.
- `docs/dev/e2e.md` §5 를 바뀐 명령대로 갱신(환자를 매번 새로 골라야 하는 이유 = 흐름의 끝이 파기, 역할 전환, **예상된 404/403 두 줄**, GIF ffmpeg 명령), §6 은 pid 주의와 실제 종료 로그.
- `docs/eval/README.md` 해석 규칙 **5–8** 추가: held-out 은 안전망 지표(목표 미달 · CI 임계값은 바닥), `purge`/`rls` 는 실행형이며 **잔여 0 / 실패율 100 %** 가 정상이라는
  읽는 방향(+ `signed_notes_surviving` 은 반대 방향), `alert_latency.json` 은 부하 A 의 산출물이라 조건이 다르다는 것, `protocol.json` 의 예제 수는 실제 실행 통계라는 것.
- **`scripts/check_links.py` 신규** — README + `docs/**/*.md` 의 상대 링크(코드 펜스 제외)를 검사한다. 현재 45개 문서, 깨진 링크 0.

## 5. 이번 패스에서 발견한 것

| 발견 | 어떻게 드러났나 | 처리 |
|---|---|---|
| **콘솔이 파기 영수증에서 403 을 받는다** — 임상의는 `revoke` 로 `purge_job_id` 를 받지만 `GET /v1/purge-jobs/{id}` 와 `verify-decrypt` 는 `{admin, auditor}` 전용이라 콘솔의 추적 폴링이 첫 요청에서 403 → 타이머 정지 | 스크린샷 드라이버가 `#verifyDecrypt` 활성화를 60 s 기다리다 실패 | RBAC 매트릭스가 의도한 동작이라 **매트릭스를 건드리지 않았다**. 드라이버가 admin 으로 재로그인하도록 고치고, 한계·버그 저널·e2e 에 "고칠 곳은 콘솔"로 기록 |
| **Playwright 번들 ffmpeg 로는 GIF 를 만들 수 없다** — `/opt/pw-browsers/ffmpeg-1011/ffmpeg-linux` 는 필터가 `scale` 뿐이고 gif 먹서도 없다 | 지시받은 명령이 `No such filter: 'fps'` → `Filter not found` 로 실패 | `uv pip install imageio-ffmpeg`(PyPI 허용, AGENT_ENV)의 전체 빌드로 변환. e2e 문서에 그대로 적었다 — §7 편차 2 |
| 데모 흐름은 **환자를 소비한다** | 두 번째 실행이 `가상환자-0004` 로 실패(이미 purged) | 실행마다 다른 환자를 쓰고 문서화(0001–0003 은 통합자 실행에서, 0004–0006 은 이번 실행에서 소비) |
| `nohup … &` 의 `$!` 가 서버 pid 가 아닐 수 있다 | SIGTERM 을 보냈는데 서버가 계속 응답 | 로그의 `Started server process [PID]` 로 종료. e2e §6 에 적었다 |

## 6. 최종 게이트

```bash
make lint                                  # ruff check clean · ruff format 313 files · mypy(strict 5 모듈) clean · pip-audit(경고만)
pytest tests/unit -q                       # 827 passed (9.7 s)
python scripts/readme_numbers.py --check   # exit 0 (docs/perf/README.md 도 exit 0)
python scripts/check_links.py              # 45개 문서, 깨진 링크 0
cd src/chartwire/eval/data && sha256sum -c FROZEN.txt                                          # OK
cd infra/cdk && python app.py && git diff --exit-code -- cdk.out/ChartwireStack.template.json  # nag 0, diff 0
```

`pip-audit` 는 `ecdsa 0.19.2` 의 `PYSEC-2026-1325` 를 보고한다(전이 의존성). Makefile 이 `|| true` 라 게이트를 막지 않으며 이번 패스에서 손대지 않았다.

## 7. 스펙·지시와 다르게 한 점 (이유)

1. **README 에 절을 하나 늘렸다** — 스펙 §14 의 항목 순서는 유지한 채 "5분 데모"를 표 ③ **뒤**, 아키텍처 **앞**에 넣었다(태스크의 "§3 위 또는 '5분 데모' 절").
   그 결과 채용공고 매핑이 README **§12** 가 된다.
2. **GIF 변환에 번들 ffmpeg 대신 전체 ffmpeg 를 썼다** — §5 표. 지시한 필터 체인(palettegen/paletteuse, 12 fps, 폭 1100)과 산출물 규격(≤ 8 MB)은 그대로 지켰다.
3. **`scripts/console_screenshots.py` 를 고쳤다** — 소유가 통합자/WP-H 이지만 드라이버 결함(403 타임아웃, 가로 스크롤)이라 캡처를 만들 수 없었다.
   콘솔(`console/index.html`)과 API·RBAC 는 **건드리지 않았다**.
4. **`Makefile` 의 `readme-numbers` 타깃에 두 줄을 더했다**(WP-A 소유 파일) — 숫자 파이프라인이 문서 세 곳을 한 번에 맞추도록. 다른 타깃은 손대지 않았다.
5. **`scripts/check_links.py` 를 새로 만들었다** — 태스크가 "작은 스크립트로 링크 검사"를 지시했고, 일회용 스크래치보다 저장소에 두는 편이 재현 가능하다.
   CI 에 넣지는 않았다(통합자 판단 사항).

## 8. 알려진 이슈 / 남은 일

- **`docs/images/demo.gif` 는 7.1 MB** 로 저장소에서 가장 큰 파일이다. 더 줄이려면 색 수(48)보다 **길이**를 줄이는 편이 낫다(`--no-shots` 로 짧은 구간만 다시 녹화).
- 스크린샷 드라이버의 `browser_errors` 에는 예상된 두 줄(`notes/latest` 404, purge-job 403)이 남고 그래서 exit 1 로 끝난다. CI 에 넣으려면 예상 오류 화이트리스트가 필요하다.
- **콘솔 UX**: 철회 → 영수증 흐름이 한 계정으로 이어지지 않는다(§5). 콘솔이 `admin` 이 아닐 때 "영수증 조회는 admin/auditor" 라고 안내하고 job id 만 보여주는 편이 낫다.
- `docs/images/04_purge_receipt.png` 는 `verify-decrypt` 의 JSON 출력이 접힘선 아래에 있다(토스트에는 보인다). 우측 패널만 스크롤해 찍으면 더 낫다.
- 시나리오 E · Anthropic tier · 수평 확장 토폴로지는 여전히 미실행이다(문서에 그렇게 적혀 있다).
- `pip-audit` 의 `ecdsa` 경고(§6).
- 데모 DB 의 `가상환자-0001..0006` 은 파기됐다. 다시 찍으려면 남은 환자(0007+)를 쓰거나 `db downgrade base && db upgrade && seed --demo`.
