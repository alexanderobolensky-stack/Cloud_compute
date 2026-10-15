"""A tiny stand-in for a local AI agent (stdlib only).

It logs its "reasoning", burns a little CPU now and then, and exposes:
  GET  /health  -> {"status": "ok"}
  POST /task    -> {"input": "..."} answered with a canned "plan"

Run: python examples/demo_agent.py --name researcher --port 8711
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

THOUGHTS = [
    "читаю контекст задачи",
    "ищу релевантные документы в локальном индексе",
    "вызываю инструмент: search_notes",
    "сжимаю историю диалога",
    "сверяю ответ с источниками",
    "жду ответ модели",
    "планирую следующий шаг",
]


def busy(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        sum(i * i for i in range(2000))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="demo")
    parser.add_argument("--port", type=int, default=8711)
    parser.add_argument("--fail-after", type=float, default=0,
                        help="exit with an error after N seconds (to demo restarts)")
    args = parser.parse_args()
    state = {"tasks": 0, "started": time.time()}
    memory: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, {"status": "ok", "agent": args.name, "tasks": state["tasks"],
                                 "uptime_s": round(time.time() - state["started"])})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/task":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                text = json.loads(self.rfile.read(length) or b"{}").get("input", "")
            except ValueError:
                return self._send(400, {"error": "invalid json"})
            state["tasks"] += 1
            print(f"[{args.name}] новая задача #{state['tasks']}: {text[:80]!r}", flush=True)
            busy(random.uniform(0.3, 1.2))
            steps = [f"{i + 1}. {random.choice(THOUGHTS)}" for i in range(3)]
            self._send(200, {"agent": args.name, "task": text,
                             "plan": steps, "answer": f"Готово: обработал «{text[:60]}» за 3 шага."})

        def log_message(self, fmt: str, *a) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[{args.name}] слушаю http://127.0.0.1:{args.port}", flush=True)

    while True:
        time.sleep(random.uniform(1.5, 4))
        print(f"[{args.name}] {random.choice(THOUGHTS)}", flush=True)
        if random.random() < 0.25:
            busy(random.uniform(0.2, 0.8))
        # Slowly grow and release a cache so the memory chart has something to show.
        if len(memory) < 40 and random.random() < 0.5:
            memory.append(bytearray(1024 * 1024))
        elif memory and random.random() < 0.2:
            del memory[: len(memory) // 2]
        if args.fail_after and time.time() - state["started"] > args.fail_after:
            print(f"[{args.name}] ошибка: потеряно соединение с векторной БД", flush=True)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
