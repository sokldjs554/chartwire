# WP-H 핸드오프 — Infra / Console / Loadtest client (Phase 0)

> 상태: **진행 중** (2026-09-03, 재개 실행). 이전 실행은 파일을 하나도 남기지 못하고 중단됨(소유 경로가 모두 비어 있었음).

## 계획 (완료 시 체크)
- [x] `infra/cdk/stack.py` + `app.py` + `cdk.json` + `requirements.txt` — §12.1 그대로, `python infra/cdk/app.py` 로 synth (`cdk.out/ChartwireStack.template.json` 커밋 대상)
- [x] `infra/cdk/nag-suppressions.md` — cdk-nag AwsSolutionsChecks ERROR 만 수정/억제, 사유 기록
- [x] `infra/cdk/tests/test_stack.py` — §12.1 assertion 목록
- [ ] `src/chartwire/loadtest/client.py` — asyncio recorder/viewer 클라이언트(§6 의미론), 통계, chaos hook
- [ ] `tests/unit/test_loadtest_client.py` — fake websocket 기반 순수 파이썬 테스트
- [ ] `console/index.html` — §13.3 전체, 단일 파일, CDN 없음
- [ ] `tests/unit/test_console_static.py` — `<script>` 추출 → `node --check`, 필수 문자열
- [ ] `render.yaml` — §12.2
- [ ] `docs/aws.md` — mermaid 다이어그램, synth-only, 30→300 sizing, 비용 스케치
- [ ] 이 문서 완성(모듈 맵, 테스트 실행법, 편차, 다른 WP 요청)

## 진행 로그
- 05:30 환경/스펙 읽기 완료. `chartwire.ws.codec` 이 이미 존재 → 로드테스트 클라이언트는 이를 임포트(별도 `frames.py` 불필요).
- 06:05 CDK 스택 synth 성공(nag ERROR 0, 결정론적 템플릿), test_stack 13 passed. cdk-nag 3.0.2 는 aws-cdk-lib 2.267 과 jsii 런타임에서 `aspect.visit is not a function` 으로 실패 → `cdk-nag==2.38.2` 로 고정(venv 에 설치).
