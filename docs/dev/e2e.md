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
chartwire simulate --script s01 --speed 4                 # 로그인(clinician) → script_ref=s01 인 created 세션 선택 → 티켓 → hello → 757 청크 → end → bye{ended}
# 세션을 직접 고르려면: chartwire simulate --script s01 --speed 4 --session <id>
# 재개 경로 시연:      chartwire simulate --script s01 --speed 4 --drop-at 10s
```

이 박스에서의 출력(요약, 숫자는 README 용이 아님): `outcome=ended, sent=757, ack_seq=757, loss=0, nacks=0, credit_min=50, finals=50,
alerts=1, note_status=verified`. 뷰어 소켓이 함께 열려 `transcript.final`/`risk.alert`/`note.status` 를 센다.

## 4. REST 로 확인 (`scripts/e2e_check.sh [session_id]` 가 아래를 그대로 실행한다)

```bash
A=http://127.0.0.1:8000
TOK=$(curl -s -X POST $A/v1/auth/token -H 'content-type: application/json' -d '{"tenant_slug":"demo","email":"clinician@demo.clinic","password":"demo1234!"}' | jq -r .access_token)
ADM=$(curl -s -X POST $A/v1/auth/token -H 'content-type: application/json' -d '{"tenant_slug":"demo","email":"admin@demo.clinic","password":"demo1234!"}' | jq -r .access_token)
H="Authorization: Bearer $TOK"; HA="Authorization: Bearer $ADM"
SID=$(curl -s "$A/v1/sessions?state=drafted&limit=1" -H "$H" | jq -r '.items[0].id')

curl -s $A/v1/sessions/$SID -H "$H" | jq '{state, ack_seq, final_seq}'                    # drafted, 757, 757
curl -s "$A/v1/sessions/$SID/segments?limit=500" -H "$H" | jq 'length'                    # 50 (seq 0..49, 복호화된 text)
curl -s "$A/v1/alerts?open=1" -H "$H" | jq '.[0] | {id, category, severity, segment_seq, sla_deadline_at}'
curl -s -X POST $A/v1/alerts/1/ack -H "$H" | jq '{acknowledged_at}'
NID=$(curl -s $A/v1/sessions/$SID/notes/latest -H "$H" | jq -r .id)
curl -s $A/v1/sessions/$SID/notes/latest -H "$H" | jq '{status, provider, coverage, statement_count, unsupported_count}'   # verified, extractive, 1.0, 14, 0
curl -s -X POST $A/v1/notes/$NID/sign -H "$H" | jq '{status, code}'                        # 409 CW-4092 (평가 없음)
curl -s -X POST $A/v1/notes/$NID/statements/14/decision -H "$H" -H 'content-type: application/json' -d '{"decision":"reject"}' > /dev/null
curl -s -X POST $A/v1/notes/$NID/statements/13/decision -H "$H" -H 'content-type: application/json' -d '{"decision":"edit","edited_text":"2주 뒤 재진 예정"}' > /dev/null
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
    --patient 가상환자-0006 --out docs/images                       # PNG 5장
python scripts/console_screenshots.py --chromium /opt/pw-browsers/chromium \
    --patient 가상환자-0007 --video-dir var/demo-video               # 같은 흐름 + WebM 녹화
# 로그인(clinician) → 세션 생성(--patient, s01) → 뷰어 연결 → 녹음 시작(×4) → 위험 배너 → ACK → 종료 → SOAP 초안
#   → 동의 철회 → **admin 으로 재로그인** → 영수증 추적 → 복호화 시도 → Ops
# 스크린샷: docs/images/01_recorder.png … 05_ops.png
```

- **환자를 매번 새로 고른다.** 흐름의 마지막이 동의 철회 + 환자 단위 파기라 한 번 쓴 환자는 `consent_state=purged` 가 되어 재사용할 수 없다
  (`가상환자-0001…0020` 중 아직 `granted` 인 것을 고른다. 다 쓰면 `chartwire db downgrade base && chartwire db upgrade && chartwire seed --demo`).
- **역할 전환은 의도된 것이다.** 임상의는 동의를 철회할 수 있지만 `GET /v1/purge-jobs/{id}` 와 `verify-decrypt` 는 `{admin, auditor}` 전용이라
  (§4 에서 `$HA` 를 쓰는 것과 같은 이유) 콘솔의 추적 폴링이 **403 을 한 번 받고** 멈춘다. 드라이버는 그 지점에서 `--admin`(기본 `admin@demo.clinic`)으로
  다시 로그인한다.
- 그래서 출력 JSON 의 `browser_errors` 에는 **예상된 두 줄**이 남는다: 초안이 아직 없을 때의 `notes/latest` 404 와 위의 403. 그 밖의 오류는 0 이어야 한다.
- GIF 만들기(12 fps, 폭 1100, ≤ 8 MB). Playwright 번들 ffmpeg 는 `scale` 필터뿐이라 **팔레트 필터도 gif 먹서도 없다** — 전체 빌드를 쓴다:

```bash
FF=$(python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")   # uv pip install imageio-ffmpeg
V=var/demo-video/*.webm
"$FF" -y -i $V -vf "fps=12,scale=1100:-2:flags=lanczos,palettegen=max_colors=48:stats_mode=diff" var/palette.png
"$FF" -y -i $V -i var/palette.png -lavfi "fps=12,scale=1100:-2:flags=lanczos[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle" \
     -loop 0 docs/images/demo.gif
```

## 6. 종료

```bash
kill -TERM <pid>   # 백그라운드 셸이 아니라 서버 프로세스 pid 로
# drain started → ws drain started → stt-worker stopped {clean:true} → worker stopped {clean:true}
# → Application shutdown complete → Finished server process
```

`serve all --embedded` 를 `nohup … &` 로 띄웠다면 `$!` 는 래퍼 셸일 수 있다 — 로그의 `Started server process [PID]` 를 쓰는 편이 확실하다.
