# infra-bot

텔레그램 대화로 홈서버 인프라 상태(CPU/메모리/디스크)를 조회하는 AI 비서. Gemini function calling으로 Prometheus 조회 도구를 호출함.

## 로컬에서 테스트 (선택)

```bash
export TELEGRAM_BOT_TOKEN=...
export GEMINI_API_KEY=...
export ALLOWED_CHAT_ID=...
export PROMETHEUS_URL=http://10.0.0.1:9090   # VPN 연결 상태에서만 접근 가능
pip install -r requirements.txt
uvicorn main:app --reload
```

## 빌드 & 배포 (라즈베리파이에서 직접 빌드 — arm64)

```bash
ssh raspi
git clone https://github.com/DOHOON0127/infra-bot.git
cd infra-bot

docker login ghcr.io -u DOHOON0127   # GitHub Personal Access Token(write:packages 권한)으로 로그인
docker build -t ghcr.io/dohoon0127/infra-bot:latest .
docker push ghcr.io/dohoon0127/infra-bot:latest
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

k3s 파드가 `10.0.0.1:9090`(Prometheus)으로 나가는 트래픽은 wg0 인터페이스를 거치지 않아서, 기존 UFW 규칙(wg0만 허용)에 걸려 막힐 수 있음. k3s 파드 네트워크 대역(기본 `10.42.0.0/16`)에서 9090으로 가는 트래픽을 허용해야 함:

```bash
kubectl cluster-info dump | grep -m1 cluster-cidr   # 정확한 대역 확인
sudo ufw allow from 10.42.0.0/16 to any port 9090 proto tcp
```

## 배포 등록

`homeserver-iac` 저장소의 `k8s-manifests/infra-bot-application.yaml`을 적용하면 ArgoCD가 이 저장소의 `k8s-manifests/deployment.yaml`을 계속 감시·동기화함:

```bash
kubectl apply -f ../homeserver-iac/k8s-manifests/infra-bot-application.yaml
```
