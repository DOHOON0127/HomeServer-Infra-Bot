# HomeServer-Infra-Bot

텔레그램 대화로 홈서버 인프라 상태(CPU/메모리/디스크)를 조회하는 AI 비서. Gemini function calling으로 Prometheus 조회 도구를 호출함. `homeserver-iac`의 k3s 클러스터 위에서 ArgoCD로 GitOps 배포됨.

## 아키텍처

```
텔레그램 ←폴링→ [FastAPI 앱, k3s 파드]
                    ├── /metrics (Prometheus가 긁어감)
                    ├── 대화 처리 로직
                    │     └── Gemini(google-genai SDK) 호출, function calling으로 도구 스키마 전달
                    └── query_prometheus 도구 → http://10.0.0.1:9090 (Docker Compose로 띄운 Prometheus)
```

## 로컬에서 테스트 (선택)

```bash
export TELEGRAM_BOT_TOKEN=...
export GEMINI_API_KEY=...
export ALLOWED_CHAT_ID=...
export PROMETHEUS_URL=http://10.0.0.1:9090   # VPN 연결 상태에서만 접근 가능
pip install -r requirements.txt
uvicorn main:app --reload
```

## 배포 — CI/CD (기본 방식)

`main` 브랜치에 push하면 GitHub Actions가 자동으로:

1. arm64 이미지 빌드 (QEMU 에뮬레이션)
2. `ghcr.io/dohoon0127/homeserver-infra-bot:<commit-sha>`로 push
3. `k8s-manifests/deployment.yaml`의 이미지 태그를 그 SHA로 갱신해 같은 저장소에 자동 커밋
4. ArgoCD가 그 변경을 감지해 자동 배포 (Pi에 SSH 접속 불필요)

**주의**: CI가 `k8s-manifests/deployment.yaml`을 자동으로 커밋하므로, 로컬에서 push 전에는 항상 `git pull`이 필요함. 이 파일은 CI가 관리하는 영역이므로 로컬에서 직접 수정하지 않는 것을 원칙으로 함.

## 배포 — 수동 (최초 셋업 또는 CI 우회 시)

```bash
ssh raspi
git clone https://github.com/DOHOON0127/HomeServer-Infra-Bot.git
cd HomeServer-Infra-Bot
docker build -t ghcr.io/dohoon0127/homeserver-infra-bot:latest .
docker push ghcr.io/dohoon0127/homeserver-infra-bot:latest
kubectl rollout restart deployment infra-bot -n infra-bot   # latest 태그는 자동 재배포 안 되므로 강제 재시작 필요
```

## 비밀값 (git에 올리지 않음 — kubectl로 직접 생성)

```bash
kubectl create namespace infra-bot
kubectl create secret generic infra-bot-secrets -n infra-bot \
  --from-literal=telegram-bot-token=<봇 토큰> \
  --from-literal=gemini-api-key=<Gemini API 키> \
  --from-literal=allowed-chat-id=<네 텔레그램 chat_id>
```

## UFW — Prometheus로 나가는 트래픽 허용 필요

k3s 파드가 `10.0.0.1:9090`(Prometheus, Docker Compose로 호스트에 직접 떠있음)으로 나가는 트래픽은 wg0 인터페이스를 거치지 않아서, 기존 UFW 규칙(wg0만 허용)에 걸려 막힘. k3s 파드 네트워크 대역에서 9090으로 가는 트래픽을 허용해야 함:

```bash
kubectl get node -o jsonpath='{.items[0].spec.podCIDR}'   # 정확한 대역 확인 (기본 10.42.0.0/16)
sudo ufw allow from 10.42.0.0/16 to any port 9090 proto tcp
```

## 배포 등록

`homeserver-iac` 저장소의 `k8s-manifests/infra-bot-application.yaml`을 적용하면 ArgoCD가 이 저장소의 `k8s-manifests/deployment.yaml`을 계속 감시·동기화함:

```bash
kubectl apply -f ../homeserver-iac/k8s-manifests/infra-bot-application.yaml
```

## 설계 결정 및 트러블슈팅

- **`google-generativeai` → `google-genai` 마이그레이션** — 초기 구현에 쓴 `google-generativeai` 패키지가 완전히 EOL 상태로, 모델명(`gemini-2.0-flash`)과 멀티턴 대화의 role 이름(`function`)이 순차적으로 백엔드에서 거부되기 시작함. 증상마다 땜질하는 대신 공식 후속 SDK(`google-genai`)로 전체 마이그레이션해 근본 해결.
- **강제 도구 호출 설정이 후속 호출까지 새어 들어간 버그** — 최초 요청에서 도구 호출을 강제(`mode: ANY`)하는 설정을, 도구 실행 결과를 텍스트로 정리하는 두 번째 호출에도 그대로 재사용해 모델이 답변 대신 도구를 또 호출하려 함(응답이 빈 값으로 반환됨). 두 번째 호출은 별도로 도구 호출을 금지(`mode: NONE`)하는 설정을 사용하도록 분리하고, 만약을 대비해 조회값을 그대로 보여주는 폴백도 추가.
- **GHCR 패키지의 Actions 접근 권한은 저장소 권한과 별개** — 저장소 Settings에서 Workflow permissions를 Read/write로 바꿔도, 개인 토큰으로 최초 push해 만들어진 패키지는 Actions에 자동으로 연결되지 않음. 패키지 Settings의 "Manage Actions access"에서 해당 저장소에 Write 권한을 별도로 부여해야 CI가 push 가능.
- **CI 자동 커밋과 로컬 작업의 동기화** — 매니페스트 이미지 태그를 CI가 자동으로 커밋하는 구조라, 로컬에서 커밋 후 바로 push하면 원격이 앞서있어 거부됨. push 전 `git pull`을 습관화하고, `deployment.yaml`은 로컬에서 직접 수정하지 않는 것을 원칙으로 정함.
- **UFW의 컨테이너/파드 트래픽 차단 패턴 재확인** — `homeserver-iac`에서 겪은 wireguard-exporter 사례(도커 브리지 트래픽이 예상 인터페이스를 거치지 않아 차단됨)와 동일한 패턴이 k3s 파드 → Prometheus 통신에서도 재현됨. 컨테이너/파드 네트워크가 관여하는 트래픽은 항상 "어느 인터페이스로 들어오는가"를 먼저 확인하는 습관으로 이어짐.

## 스택

FastAPI · google-genai (Gemini function calling) · Prometheus · Docker · k3s · ArgoCD · GitHub Actions (CI/CD) · GHCR
