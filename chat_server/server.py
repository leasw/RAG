"""로컬 채팅 웹 UI. AgentRuntime을 그대로 감싼 stdlib 전용 HTTP 서버다.

    python -m chat_server.server            # http://localhost:8812

외부 웹 프레임워크(Flask/FastAPI)가 설치돼 있지 않아 http.server만으로 짰다.
CLI(`python -m org_agent_mvp`)의 interactive() 모드와 동일하게, 서버가 뜬 동안
AgentRuntime 인스턴스 하나를 계속 재사용한다 — 그래야 SessionRecorder가 같은
세션으로 쌓여서 "아까 그거" 같은 대화 연속성이 CLI 때와 똑같이 동작한다.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"

sys.path.insert(0, str(ROOT))

from org_agent_mvp.agent_runtime import AgentRuntime  # noqa: E402
from org_agent_mvp.config import AppConfig  # noqa: E402
from org_agent_mvp.fact_extractor import FactExtractor  # noqa: E402
from org_agent_mvp.fact_memory import FactMemory  # noqa: E402
from org_agent_mvp.openrouter_client import OpenRouterClient  # noqa: E402
from org_agent_mvp.session_recorder import SessionRecorder  # noqa: E402

PORT = 8812

# 다시 ThreadingHTTPServer로 돌린다. 한때 단일 스레드로 바꾼 적이 있는데, 그러면
# 응답이 오래 걸리는 요청(POST /api/chat, 특히 응답 뒤 백그라운드로 도는 STM
# 기록·승격 판정) 하나가 서버의 유일한 처리 스레드를 붙잡는 동안 GET /api/stats
# 처럼 완전히 무관한 요청까지 전부 멈췄다 — "질문 보냈는데 응답이 하염없이 안 옴"
# 증상의 실제 원인이었다. FactMemory의 sqlite3 커넥션을 check_same_thread=False로
# 열어뒀으니(fact_memory.py 참고) 이제 여러 스레드가 그 커넥션을 만져도 죽지
# 않는다 — 실제 동시 접근이 겹칠 상황만 _lock으로 직렬화하면 된다.
_lock = threading.Lock()
_runtime: AgentRuntime | None = None


def get_runtime() -> AgentRuntime:
    global _runtime
    if _runtime is None:
        config = AppConfig.load()
        client = OpenRouterClient(config)
        memory = FactMemory(config.memory_root)
        recorder = SessionRecorder(memory=memory, extractor=FactExtractor(client))
        _runtime = AgentRuntime(config=config, client=client, memory=memory, recorder=recorder)
    return _runtime


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0: 응답마다 연결을 닫는다. 단일 스레드 서버에서 HTTP/1.1 keep-alive를
    # 쓰면, 브라우저 탭 하나가 연결을 열어둔 채 다음 요청을 안 보내는 순간 서버의
    # 유일한 서빙 스레드가 그 소켓의 다음 요청 줄을 기다리며 영원히 막힌다 — 새
    # 연결은 OS 큐에 쌓이기만 하고 절대 처리되지 않는다("서버가 안 죽었는데도
    # 응답이 안 오는" 증상의 실제 원인이었다). 로컬 1인용 툴이라 연결 재사용 이득이
    # 없으니, 매 요청마다 닫아서 이 교착을 원천 차단한다.
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):  # 콘솔 소음 억제, 필요하면 지운다
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif self.path == "/api/stats":
            try:
                # 일부러 _lock을 안 잡는다. 답변 뒤에 도는 백그라운드 finish()가
                # STM 기록용 LLM 재호출(최대 90초)까지 포함해서 _lock을 오래
                # 붙잡는데, 여길 그 락으로 감싸면 사용자가 그 사이에 새로고침만
                # 해도 이 단순 집계 조회 하나 때문에 화면이 그만큼 멈춰 보인다.
                # sqlite 스레드 크래시는 FactMemory의 check_same_thread=False로
                # 이미 해결됐고, 집계 읽기 하나가 어쩌다 쓰기와 겹쳐도 치명적이지
                # 않으므로 이 조회는 락 없이 바로 한다.
                runtime = get_runtime()
                stats = runtime.memory.stats()
                self._send_json(200, {"ok": True, "stats": stats})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        else:
            self.send_response(404)
            self.end_headers()

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_response(404)
            self.end_headers()
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid JSON"})
            return

        question = str(data.get("message", "")).strip()
        if not question:
            self._send_json(400, {"ok": False, "error": "message가 비어 있습니다"})
            return

        with _lock:
            try:
                runtime = get_runtime()
                # defer_post=True: 답변이 나오면 바로 돌려주고, STM 기록·승격
                # 확인(추가 LLM 호출 + 리랭커 첫 로드로 몇 분씩 걸릴 수 있는
                # 뒷정리)은 응답을 보낸 뒤 백그라운드에서 마무리한다. 그대로
                # 동기로 다 하면 답변이 이미 준비됐는데도 사용자가 그 뒷정리
                # 끝날 때까지 화면에서 계속 기다리게 된다.
                result = runtime.run(question, defer_post=True)
            except Exception as exc:  # noqa: BLE001 - 한 턴 실패가 서버를 죽이면 안 된다
                self._send_json(200, {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                return

        trace = result.get("trace", {})
        self._send_json(200, {
            "ok": True,
            "answer": result.get("answer", ""),
            "sources": trace.get("final_sources", []),
            "tool_calls": [
                {"tool": t["tool"], "tier": t["tier"], "query": t["query"],
                 "result_count": t["result_count"]}
                for t in trace.get("tool_calls", [])
            ],
        })

        finish = result.get("finish")
        if finish:
            def run_finish():
                with _lock:
                    try:
                        finish()
                    except Exception as exc:  # noqa: BLE001 - 뒷정리 실패가 서버를 죽이면 안 된다
                        print(f"[finish] {type(exc).__name__}: {exc}")
            threading.Thread(target=run_finish, daemon=True).start()


def main() -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    httpd.daemon_threads = True  # 메인 프로세스 종료 시 처리 중이던 요청 스레드도 같이 정리
    print(f"채팅 서버: http://127.0.0.1:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
