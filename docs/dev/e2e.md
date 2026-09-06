# End-to-end 데모 흐름 (통합자가 실제로 실행한 명령)

> 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. 아래 순서는 2026-09-04 이 박스에서 그대로 실행해 확인한 것이다
> (스펙 §15 통합 순서 1–4). `make demo` 가 1–2단계를 한 번에 한다.

## 0. 환경

```bash
source /home/user/.venvs/proj/bin/activate
set -a; . ./.env.example; set +a          # 개발 DB `chartwire`, Redis 0, localfs ./var/objects, 스크립트 var/scripts
service postgresql status || service postgresql start
redis-cli ping || redis-server --daemonize yes
```

## 1. 역할 · 스키마 · 데모 시드 (멱등)

```bash
chartwire db bootstrap-roles && chartwire db upgrade && chartwire seed --demo --if-empty
# → tenant demo: 사용자 5(<role>@demo.clinic / demo1234!), 환자 20(가상환자-0001..0020), 세션 20(created, s01..s20), var/scripts/s01..s20.json
```

개발 DB 가 마이그레이션 파일 수정 **이전**에 만들어졌다면(`permission denied for sequence audit_events_id_seq` 로 로그인 500)
`chartwire db downgrade base && chartwire db upgrade && chartwire seed --demo` 로 다시 만든다 — 개발 DB 는 버려도 되는 데이터다.

## 2. 서버 (api + worker + stt-worker 한 프로세스)

```bash
chartwire serve all --embedded --host 127.0.0.1 --port 8000 &      # = make demo
curl -s http://127.0.0.1:8000/readyz      # {"status":"ready","checks":{"postgres":"ok","redis":"ok"},...}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/console   # 200
```

## 3. 녹음기 시뮬레이션 (실제 WS 프로토콜)

```bash
chartwire simulate --script s02 --speed 4                 # 로그인(clinician) → script_ref=s02 인 created 세션 선택 → 티켓 → hello → 872 청크 → end → bye{ended}
# 세션을 직접 고르려면: chartwire simulate --script s02 --speed 4 --session <id>
# 재개 경로 시연:      chartwire simulate --script s02 --speed 4 --drop-at 10s
# (s02 = 재진 약물조정, 예상 경보 3건. s01 은 초진 우울로 위험 발화가 없어 §4 의 경보 단계가 비어 있다 — index.json 의 n_expected_alerts 로 고른다)
```

이 박스에서의 출력(요약, 숫자는 README 용이 아님): `outcome=ended, sent=872, ack_seq=872, loss=0, nacks=0, credit_min=50, finals=57,
alerts=3, note_status=verified`. 뷰어 소켓이 함께 열려 `transcript.final`/`risk.alert`/`note.status` 를 센다.

## 4. REST 로 확인 (`scripts/e2e_check.sh [session_id]` 가 아래를 그대로 실행한다)

```bash
A=http://127.0.0.1:8000
TOK=$(curl -s -X POST $A/v1/auth/token -H 'content-type: application/json' -d '{"tenant_slug":"demo","email":"clinician@demo.clinic","password":"demo1234!"}' | jq -r .access_token)
ADM=$(curl -s -X POST $A/v1/auth/token -H 'content-type: application/json' -d '{"tenant_slug":"demo","email":"admin@demo.clinic","password":"demo1234!"}' | jq -r .access_token)
H="Authorization: Bearer $TOK"; HA="Authorization: Bearer $ADM"
SID=$(curl -s "$A/v1/sessions?state=drafted&limit=1" -H "$H" | jq -r '.items[0].id')

curl -s $A/v1/sessions/$SID -H "$H" | jq '{state, ack_seq, final_seq}'                    # drafted, 872, 872
curl -s "$A/v1/sessions/$SID/segments?limit=500" -H "$H" | jq 'length'                    # 57 (seq 0..56, 복호화된 text)
# 경보 id 와 statement id 는 DB identity 값이라 시드를 다시 하지 않는 한 1 부터 시작하지 않는다 — 항상 응답에서 뽑는다
# (scripts/e2e_check.sh 가 하는 것과 같다: `.statements[-1]` / `.statements[-2]`).
AID=$(curl -s "$A/v1/alerts?open=1" -H "$H" | jq -r --arg s "$SID" '[.[] | select(.session_id==$s)][0].id')
curl -s "$A/v1/alerts?open=1" -H "$H" | jq '.[0] | {id, category, severity, segment_seq, sla_deadline_at}'
curl -s -X POST $A/v1/alerts/$AID/ack -H "$H" | jq '{acknowledged_at}'
NOTE=$(curl -s $A/v1/sessions/$SID/notes/latest -H "$H"); NID=$(jq -r .id <<<"$NOTE")
LAST=$(jq -r '.statements[-1].id' <<<"$NOTE"); PREV=$(jq -r '.statements[-2].id' <<<"$NOTE")
jq '{status, provider, coverage, statement_count, unsupported_count}' <<<"$NOTE"   # verified, extractive, 1.0, <스크립트마다 다름>, 0
curl -s -X POST $A/v1/notes/$NID/sign -H "$H" | jq '{status, code}'                        # 409 CW-4092 (평가 없음)
curl -s -X POST $A/v1/notes/$NID/statements/$LAST/decision -H "$H" -H 'content-type: application/json' -d '{"decision":"reject"}' > /dev/null
curl -s -X POST $A/v1/notes/$NID/statements/$PREV/decision -H "$H" -H 'content-type: application/json' -d '{"decision":"edit","edited_text":"2주 뒤 재진 예정"}' > /dev/null
curl -s -X PUT  $A/v1/notes/$NID/assessment -H "$H" -H 'content-type: application/json' -d '{"text":"임상가 평가(합성 데모)"}' > /dev/null
curl -s -X POST $A/v1/notes/$NID/sign -H "$H" | jq '{status, legal_hold, retention_until}'  # signed, medical_record, +10년

PID=$(curl -s $A/v1/sessions/$SID -H "$H" | jq -r .patient_id)
CID=$(curl -s $A/v1/patients/$PID/consents -H "$H" | jq -r '.[0].id')
JOB=$(curl -s -X POST $A/v1/consents/$CID/revoke -H "$H" -H 'content-type: application/json' -H "Idempotency-Key: e2e-$CID" -d '{"reason":"e2e demo"}' | jq -r .purge_job_id)
sleep 3; curl -s $A/v1/purge-jobs/$JOB -H "$HA" | jq '{state, receipt_hash_valid, steps: [.steps[].step], counts}'   # verified, true, capture/redis/objectstore/rows/crypto_shred/patient_shred
curl -s -X POST $A/v1/purge-jobs/$JOB/verify-decrypt -H "$HA" | jq .   # {"decrypt_attempted":true,"unwrap":"failed:dek_destroyed","decrypt_sample":"failed:invalid_tag","job_state":"verified"}
curl -s $A/v1/sessions/$SID/segments -H "$H" | jq '{status, code}'      # 410 CW-4100
curl -s $A/v1/notes/$NID -H "$H" | jq '{status, legal_hold, statement_count, quote: .statements[0].evidence[0].quote, assessment: .assessment.text}'   # 서명 노트는 기록 키로 그대로 읽힌다
curl -s $A/v1/patients/$PID -H "$H" | jq '{pseudonym, name, consent_state}'   # name null, purged
```

## 5. 콘솔 (Playwright) — 스크린샷과 데모 GIF

```bash
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
python scripts/console_screenshots.py --chromium /opt/pw-browsers/chromium \
    --patient 가상환자-0006 --out docs/images                       # PNG 6장
python scripts/console_screenshots.py --chromium /opt/pw-browsers/chromium \
    --patient 가상환자-0007 --video-dir var/demo-video               # 같은 흐름 + WebM 녹화
# 홈 화면 → 로그인(clinician) → 세션 생성(--patient, --script auto = 카탈로그에서 예상 경보가 있는 첫 대본; 녹음 길이는 그 대본의 total_ms)
#   → 뷰어 연결 → 녹음 시작(×4) → 위험 배너 → ACK → 종료 → SOAP 초안(note.status 가 오면 콘솔이 자동으로 불러온다)
#   → 동의 철회 → **admin 으로 재로그인** → 영수증 추적 → 복호화 시도 → Ops
# 스크린샷: docs/images/00_intro.png … 05_ops.png
```

콘솔이 보여 주는 것(§13.3 위에 얹은 안내 층):

- **홈 화면**(로그인 전, 헤더의 "홈"으로 언제든 돌아온다) — 환영 문구와 무엇을 만든 것인지, **기능 검색**(검색어로 기능·대본 카드를 거르고
  Enter 로 첫 결과에 들어간다), 자동으로 넘어가는 **배너** 4장(영수증 · 녹음 · 경보 · 대본), **서비스 카드** 8장(녹음 · 라이브 · 초안 · 철회 ·
  복호화 · 운영 지표 · 대본 고르기 · API 문서), **주호소별 대본 카드**(`/console/scripts.json` 으로 템플릿마다 대본과 예상 경보 수를 붙인다),
  5분 투어 5단계, 데모 계정 4개, 무료 인스턴스 주의사항. 카드·배너·검색으로 들어가면 필요한 계정(④·⑤·운영 지표는 admin)으로 자동 로그인하고
  작업 화면(Recorder · Live · SOAP · 사이드 패널)이 열린다. "데모 계정으로 시작"은 clinician 자격을 채우고 로그인한다.
- **단계 표시**(헤더 아래 1~5) — 녹음 종료, 경보 ACK, 초안 서명, 영수증 검증, 복호화 실패가 각각 단계를 채운다. 누르면 해당 탭으로 간다.
- **세션 선택기** — `GET /v1/sessions?limit=200` 으로 **모든 상태**를 나열하고(created 가 위, 끝난 세션도 남아 초안·영수증을 다시 볼 수 있다),
  `/console/scripts.json`(`seed --demo` 가 쓴 `index.json` 의 메타데이터)에서 대본 템플릿·예상 경보 수를 붙인다. Start 는 `created` 세션에서만 켜진다.
- **파기 영수증**은 `PurgeReceiptOut` 을 사람이 읽는 형식으로 그린다: 대상·사유·시각, 지운 것 합계(`counts`), 대상별 단계(`steps` —
  환자 단위 파기는 세션마다 capture → redis → objectstore → rows → crypto_shred 를 반복한다), 검증 항목(`verify_result.checks`), `receipt_hash` 와
  재계산 일치 여부, DEK 지문. 원본 JSON 은 접혀 있다. **복호화 시도**는 `unwrap` / `decrypt_sample` 판정표로 보여 주며 실패가 정답이다.

- **환자를 매번 새로 고른다.** 흐름의 마지막이 동의 철회 + 환자 단위 파기라 한 번 쓴 환자는 `consent_state=purged` 가 되어 재사용할 수 없다
  (`가상환자-0001…0020` 중 아직 `granted` 인 것을 고른다. 다 쓰면 `chartwire db downgrade base && chartwire db upgrade && chartwire seed --demo`).
- **역할 전환은 의도된 것이다.** 임상의는 동의를 철회할 수 있지만 `GET /v1/purge-jobs/{id}` 와 `verify-decrypt` 는 `{admin, auditor}` 전용이라
  (§4 에서 `$HA` 를 쓰는 것과 같은 이유) 콘솔의 추적 폴링이 **403 을 한 번 받고** 멈춘다. 드라이버는 그 지점에서 `--admin`(기본 `admin@demo.clinic`)으로
  다시 로그인한다.
- 그래서 출력 JSON 의 `browser_errors` 에는 **예상된 두 줄**이 남는다: 초안이 아직 없을 때의 `notes/latest` 404 와 위의 403. 그 밖의 오류는 0 이어야 한다.
  (404 는 이제 콘솔이 "아직 초안이 없습니다 — 녹음을 끝내면 워커가 만듭니다" 로 안내하고, `note.status` 가 오면 자동으로 불러온다.)
- **한 번 파기된 환자는 이름으로 찾을 수 없다.** `patient_shred` 가 이름 블라인드 인덱스를 지우므로 `--patient` 로 같은 가명을 다시 주면
  `findPatient` 가 빈 결과를 돌려주고 드라이버가 `#patientInfo` 에서 멈춘다 — 위의 "환자를 매번 새로 고른다" 가 그 이유다.
- GIF 만들기(10 fps, 폭 1100, 팔레트 40색, ≤ 8 MB — 46 초 녹화가 7.1 MB). Playwright 번들 ffmpeg 는 `scale` 필터뿐이라 **팔레트 필터도 gif 먹서도 없다** — 전체 빌드를 쓴다:

```bash
FF=$(python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")   # uv pip install imageio-ffmpeg
V=var/demo-video/*.webm
"$FF" -y -i $V -vf "fps=10,scale=1100:-2:flags=lanczos,palettegen=max_colors=40:stats_mode=diff" var/palette.png
"$FF" -y -i $V -i var/palette.png -lavfi "fps=10,scale=1100:-2:flags=lanczos[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle" \
     -loop 0 docs/images/demo.gif
```

## 6. 데모 대본 20개는 모두 같은 자격이다

`seed --demo` 는 `s01`–`s20` 대본을 디스크에 쓰고 **대본마다 `created` 세션을 하나씩** 만든다
(`synth/seed.py`, 환자 20명에 배정). 콘솔의 세션 선택기와 `script_ref` 드롭다운은 20개를 모두
보여 주고, 각 항목 옆의 템플릿·예상 경보 수는 `/console/scripts.json`(시드가 쓴 `index.json`)에서
온다. 어느 것을 골라도 서버의 STT 시뮬레이터가 그 대본을 재생하므로 전사·경보·초안이 달라진다.

전수 확인 방법 — 각 `created` 세션을 그 세션의 `script_ref` 로 재생하고 결과를 모은다:

```bash
chartwire simulate --script "$REF" --speed 8 --session "$SID" --api http://127.0.0.1:8000
curl -s -H "authorization: Bearer $TOKEN" ".../v1/sessions/$SID/notes/latest" | jq '{status, coverage, statement_count}'
```

대본은 8개 주호소 템플릿(초진 우울 · 재진 약물조정 · 불안/공황 · 불면 · 성인 ADHD 추적 · 적응/스트레스 ·
알코올 · 강박)을 **순환 배정**해(`s01` = 초진 우울 … `s08` = 강박, `s09` = 초진 우울 …) seed 1 로 생성한다
(`synth/scripts.py`). 주호소는 인사말만이 아니라 대화 전체를 바꾼다(`synth/grammar.py`): 인사·주호소 단계에서
임상의가 주호소별 질문을 던지고 환자가 그 주호소의 답을 2–3개 하며, 초점 단계(불면이면 수면, 알코올이면 음주,
재진이면 약물)는 3–4문장으로 깊게, 관계없는 단계는 한 줄 대답으로 짧게 지나가고, 초진은 "지금 드시는 약이
있으세요?"·재진은 "지난번 이후로 어떠셨어요?"로 시작하며, 계획에는 주호소별 항목(수면일지 · 간 기능 검사 ·
노출·반응방지 치료 의뢰 · 항우울제 첫 처방 …)이 반드시 하나 들어간다. 그래서 발화 수 47–60, 기대 경보 0–3건으로
갈리고, 초안의 S 도 주호소를 따라 달라진다. 경보가 0건인 대본은 고장이 아니라 **위험 발화가 없는 진료**이며,
`index.json` 의 `n_expected_alerts` 가 그 값을 미리 알려 준다.

한 번 전수로 돌려 본 결과(빈 DB, `--speed 8`, 이 저장소의 개발 박스, 주호소별 대화 생성기 이후): 20개 대본 모두
`drafted` 까지 도달했고 전사 세그먼트 47–60(대본의 발화 수와 같음), 초안 `verified` 20/20, coverage 1.0, 미검증 문장 0,
청크 손실 0 · 중복 0, 뷰어 소켓이 센 경보 수와 REST 의 경보 수가 20개 모두 같았다.
**경보 수는 20개 모두 `n_expected_alerts` 와 일치**했다(0건 12개 · 1건 5개 · 2건 2개 · 3건 1개; 범주는 자살 사고 · 타해 ·
급성 물질 사용). 같은 실행에서 검색 색인 1,061행(리댁션 토큰이 들어간 행 66개)과, API 로 복호화해 다시 읽은 노트 문장 304개 · 근거 인용
304개(19 세션 — 나머지 한 세션은 그 사이 스크린샷 흐름이 파기했다)에 전화번호·주소가 남아 있지 않았다(§10.2 리댁션, `core/pii.py`). 이 수치는 회귀 점검용이지 성능 측정이 아니다 — 부하 수치는
README 표 ①. 실행기는 `created` 세션마다 `chartwire simulate --script <ref> --session <id> --speed 8` 을 돌리고
`notes/latest` · `/v1/alerts?open=1` · `segments` 를 모아 대본별 한 줄로 남긴다.

## 7. 종료

```bash
kill -TERM <pid>   # 백그라운드 셸이 아니라 서버 프로세스 pid 로
# drain started → ws drain started → stt-worker stopped {clean:true} → worker stopped {clean:true}
# → Application shutdown complete → Finished server process
```

`serve all --embedded` 를 `nohup … &` 로 띄웠다면 `$!` 는 래퍼 셸일 수 있다 — 로그의 `Started server process [PID]` 를 쓰는 편이 확실하다.
