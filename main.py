import logging
import os
import threading
import time

import requests
from fastapi import FastAPI
from fastapi.responses import Response
from google import genai
from google.genai import types
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)
logger = logging.getLogger("infra-bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])

MODEL_NAME = "gemini-3.6-flash"
client = genai.Client(api_key=GEMINI_API_KEY)

MESSAGES_TOTAL = Counter("infra_bot_messages_total", "Total telegram messages processed")
TOOL_CALLS_TOTAL = Counter(
    "infra_bot_tool_calls_total", "Tool calls", ["tool", "status"]
)
GEMINI_LATENCY = Histogram("infra_bot_gemini_latency_seconds", "Gemini call latency")

app = FastAPI()


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# 자유 PromQL 실행을 허용하지 않고, 미리 정의된 안전한 쿼리만 화이트리스트로 노출함
SAFE_QUERIES = {
    "cpu": '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
    "memory": "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100",
    "disk": '(node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100',
}


def query_prometheus(metric: str) -> str:
    if metric not in SAFE_QUERIES:
        TOOL_CALLS_TOTAL.labels(tool="query_prometheus", status="rejected").inc()
        return f"허용되지 않은 지표임: {metric}. 가능한 값: {list(SAFE_QUERIES.keys())}"
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": SAFE_QUERIES[metric]},
            timeout=5,
        )
        resp.raise_for_status()
        result = resp.json()["data"]["result"]
        if not result:
            TOOL_CALLS_TOTAL.labels(tool="query_prometheus", status="empty").inc()
            return f"{metric} 데이터 없음"
        value = float(result[0]["value"][1])
        TOOL_CALLS_TOTAL.labels(tool="query_prometheus", status="ok").inc()
        return f"{metric}: {value:.1f}%"
    except Exception as exc:
        logger.error("prometheus query failed: %s", exc)
        TOOL_CALLS_TOTAL.labels(tool="query_prometheus", status="error").inc()
        return f"조회 실패: {exc}"


QUERY_PROMETHEUS_DECLARATION = types.FunctionDeclaration(
    name="query_prometheus",
    description="라즈베리파이 서버의 실시간 리소스 사용량(CPU, 메모리, 디스크)을 조회함",
    parameters={
        "type": "object",
        "properties": {
            "metric": {
                "type": "string",
                "enum": ["cpu", "memory", "disk"],
                "description": "조회할 지표",
            }
        },
        "required": ["metric"],
    },
)
PROMETHEUS_TOOL = types.Tool(function_declarations=[QUERY_PROMETHEUS_DECLARATION])

# 이 키워드가 있으면 Gemini의 자유 판단(AUTO) 대신 도구 호출을 강제함(ANY).
# 애매한 표현에서 도구 호출을 누락하는 걸 막기 위한 안전장치.
INFRA_KEYWORDS = ["cpu", "메모리", "디스크", "서버 상태", "용량"]


def handle_message(text: str) -> str:
    MESSAGES_TOTAL.inc()

    force_tool = any(keyword in text.lower() for keyword in INFRA_KEYWORDS)
    tool_config = types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(
            mode="ANY" if force_tool else "AUTO"
        )
    )
    config = types.GenerateContentConfig(tools=[PROMETHEUS_TOOL], tool_config=tool_config)

    start = time.time()
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=text,
        config=config,
    )
    GEMINI_LATENCY.observe(time.time() - start)

    function_calls = response.function_calls or []
    if function_calls and function_calls[0].name == "query_prometheus":
        fc = function_calls[0]
        tool_result = query_prometheus(fc.args["metric"])

        function_response_part = types.Part.from_function_response(
            name=fc.name,
            response={"result": tool_result},
        )
        follow_up = client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                types.Content(role="user", parts=[types.Part.from_text(text=text)]),
                response.candidates[0].content,
                types.Content(role="user", parts=[function_response_part]),
            ],
            config=config,
        )
        return follow_up.text

    return response.text


def telegram_poll_loop():
    offset = None
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35,
            )
            updates = resp.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                text = message.get("text", "")
                if chat_id != ALLOWED_CHAT_ID or not text:
                    continue
                try:
                    reply = handle_message(text)
                except Exception as exc:
                    logger.error("handle_message failed: %s", exc)
                    reply = "처리 중 오류가 발생함"
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": reply},
                    timeout=10,
                )
        except Exception as exc:
            logger.error("poll loop error: %s", exc)
            time.sleep(5)


@app.on_event("startup")
def startup():
    thread = threading.Thread(target=telegram_poll_loop, daemon=True)
    thread.start()
    logger.info("telegram polling started")
