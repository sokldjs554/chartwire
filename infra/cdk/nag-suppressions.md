# cdk-nag 억제 목록 (`AwsSolutionsChecks`, ERROR 만 대상)

`python infra/cdk/app.py` 는 `AwsSolutionsChecks(verbose=True)` 를 적용하고 ERROR 가 하나라도 남으면 종료 코드 1 을 돌려줍니다.
`tests/test_stack.py::test_cdk_nag_has_no_errors` 가 같은 조건을 검사합니다. WARNING 은 수정 대상이 아닙니다(스펙 §12.1).

## 수정한 항목 (억제하지 않음)

| 규칙 | 리소스 | 조치 |
|---|---|---|
| AwsSolutions-RDS11 | `Db` | 기본 포트 대신 5433 (`PG_PORT`) |
| AwsSolutions-AEC5 | `Redis` | 기본 포트 대신 6380 (`REDIS_PORT`) |
| AwsSolutions-ELB2 | `Alb` | 접근 로그를 `AlbLogs` 버킷(SSE-S3, 30 일 만료)에 기록 |
| AwsSolutions-VPC7 | `Vpc` | VPC Flow Log → CloudWatch Logs (30 일) |
| AwsSolutions-RDS3/RDS10 | `Db` | Multi-AZ, 삭제 보호 |
| AwsSolutions-AEC3/AEC4/AEC6 | `Redis` | 전송·저장 암호화, Multi-AZ 자동 failover, AUTH 토큰(Secrets Manager) |
| AwsSolutions-ECS4 | `Cluster` | Container Insights 활성 |
| AwsSolutions-SNS3 | `Alarms` | 토픽 정책에 TLS 강제 |
| AwsSolutions-S10 | 두 버킷 | `enforce_ssl=True` |
| CloudFormation E3005 | `Https` | 조건부 443 리스너를 L1 로 만들어 ECS 서비스가 조건부 리소스에 `DependsOn` 을 걸지 않게 함 |

## 억제한 항목과 사유 (`stack.py::_suppress_nag`)

| 규칙 | 리소스 | 사유 |
|---|---|---|
| AwsSolutions-EC23 | `AlbSg` | 공개 ALB 의 80/443 을 0.0.0.0/0 에서 받는 것이 제품 표면입니다(의원 녹음기의 WebSocket). 뒤의 앱 SG 는 ALB SG 에서만 8000 을 허용합니다. |
| AwsSolutions-S1 | `Audio`, `AlbLogs` | 오디오 버킷의 서버 접근 로그는 `{tenant}/{session}/{seq}.bin` 키 접근 패턴을 두 번째 버킷에 복제합니다. 감사 경로는 CloudTrail S3 데이터 이벤트로 계획합니다. `AlbLogs` 는 그 자체가 로그 싱크입니다. |
| AwsSolutions-SMG4 | `JwtSecret`, `RedisAuth`, `Db/Secret` | 키·비밀 회전은 범위 밖입니다(스펙 §8.2). JWT 비밀을 회전하면 이중 키 창 없이는 15 분짜리 유효 토큰이 즉시 무효가 됩니다. |
| AwsSolutions-ECS2 | `Task-api`, `Task-worker`, `Task-stt-worker` | 환경 변수에는 엔드포인트·포트·기능 플래그만 있습니다. 모든 자격 증명은 `secrets` 맵(Secrets Manager)으로 주입됩니다. |
| AwsSolutions-IAM5 | `TaskRole/DefaultPolicy` | `s3:PutObject/GetObject/DeleteObject` 를 `bucket/*` 에 허용합니다. 키는 세션마다 새로 생성되므로 오브젝트 단위 ARN 을 미리 열거할 수 없고, 파기 파이프라인이 접두사 단위로 삭제해야 합니다. KMS 권한은 `kms:GenerateDataKey/Decrypt` 두 개, KEK 한 키로 한정됩니다(`test_task_role_is_least_privilege`). |

## 남아 있는 WARNING (수정 의무 없음)

`aws-cdk-lib` 의 `CfnResource#addDependency` deprecation 경고는 라이브러리 내부(L2 Vpc) 에서 나오며 스택 코드와 무관합니다.
