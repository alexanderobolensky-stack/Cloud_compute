"""A minimal agent backed by a local Ollama model (stdlib only).

  GET  /health  -> 200 when Ollama answers and the model exists, 503 otherwise
  POST /task    -> {"input": "..."}; the model's answer streams into the logs

Settings (env or flags): OLLAMA_URL (default http://127.0.0.1:11434),
MODEL (default: the first installed qwen model, else the first model),
SYSTEM_PROMPT, --port.

Run: python examples/ollama_agent.py --port 8720
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_SYSTEM = "Ты полезный ассистент. Отвечай по-русски, кратко и по делу."
HISTORY_TURNS = 6  # remembered question/answer pairs


def log(message: str) -> None:
    print(message, flush=True)


def ollama(base: str, path: str, payload: dict | None = None, timeout: float = 10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def installed_models(base: str) -> list[str]:
    with ollama(base, "/api/tags") as resp:
        return [m["name"] for m in json.load(resp).get("models", [])]


def pick_model(base: str, wanted: str | None) -> str | None:
    models = installed_models(base)
    if wanted:
        return wanted if wanted in models or f"{wanted}:latest" in models else None
    return next((m for m in models if "qwen" in m.lower()), models[0] if models else None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8720)))
    parser.add_argument("--model", default=os.environ.get("MODEL") or None)
    parser.add_argument("--ollama", default=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"))
    args = parser.parse_args()
    base = args.ollama.rstrip("/")
    system = os.environ.get("SYSTEM_PROMPT", DEFAULT_SYSTEM)
    state = {"model": None, "history": [], "tasks": 0}
    lock = threading.Lock()

    def resolve_model() -> str | None:
        try:
            model = pick_model(base, args.model)
        except (urllib.error.URLError, OSError) as exc:
            log(f"[agent] Ollama недоступна ({base}): {exc}")
            return None
        if model and model != state["model"]:
            log(f"[agent] модель: {model}")
        elif not model:
            log(f"[agent] ошибка: модель {args.model or '(любая)'} не найдена в Ollama — выполните ollama pull")
        state["model"] = model
        return model

    def run_task(text: str) -> dict:
        model = state["model"] or resolve_model()
        if not model:
            raise RuntimeError("модель недоступна")
        messages = [{"role": "system", "content": system}, *state["history"], {"role": "user", "content": text}]
        log(f"[agent] задача #{state['tasks']}: {text[:200]}")
        started = time.time()
        answer, line, stats = [], "", {}
        with ollama(base, "/api/chat", {"model": model, "messages": messages, "stream": True}, timeout=600) as resp:
            for raw in resp:
                chunk = json.loads(raw)
                if chunk.get("error"):
                    raise RuntimeError(chunk["error"])
                piece = (chunk.get("message") or {}).get("content", "")
                answer.append(piece)
                line += piece
                while "\n" in line:  # stream the answer into the portal logs line by line
                    head, line = line.split("\n", 1)
                    log(f"  {head}")
                if chunk.get("done"):
                    stats = chunk
        if line:
            log(f"  {line}")
        text_out = "".join(answer).strip()
        tokens = stats.get("eval_count")
        speed = round(tokens / (stats.get("eval_duration", 0) / 1e9), 1) if tokens and stats.get("eval_duration") else None
        elapsed = round(time.time() - started, 1)
        log(f"[agent] готово за {elapsed} с, токенов: {tokens}, {speed} ток/с")
        state["history"] = (state["history"] + [{"role": "user", "content": text},
                                                {"role": "assistant", "content": text_out}])[-2 * HISTORY_TURNS:]
        return {"model": model, "answer": text_out, "elapsed_s": elapsed, "tokens": tokens, "tokens_per_s": speed}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path != "/health":
                return self._send(404, {"error": "not found"})
            try:
                ok = state["model"] in installed_models(base) if state["model"] else bool(resolve_model())
            except (urllib.error.URLError, OSError):
                ok = False
            self._send(200 if ok else 503, {"status": "ok" if ok else "model unavailable",
                                            "model": state["model"], "tasks": state["tasks"]})

        def do_POST(self) -> None:
            if self.path == "/reset":
                state["history"] = []
                return self._send(200, {"status": "history cleared"})
            if self.path != "/task":
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                text = str(json.loads(self.rfile.read(length) or b"{}").get("input", "")).strip()
            except ValueError:
                return self._send(400, {"error": "invalid json"})
            if not text:
                return self._send(400, {"error": "empty input"})
            with lock:  # one generation at a time; local models are slow to share
                state["tasks"] += 1
                try:
                    self._send(200, run_task(text))
                except (urllib.error.URLError, OSError, RuntimeError, ValueError) as exc:
                    log(f"[agent] ошибка: {exc}")
                    self._send(502, {"error": str(exc)})

        def log_message(self, fmt: str, *a) -> None:
            pass

    resolve_model()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        log(f"[agent] ошибка: порт {args.port} занят ({exc})")
        sys.exit(1)
    log(f"[agent] слушаю http://127.0.0.1:{args.port}, Ollama: {base}")
    server.serve_forever()


if __name__ == "__main__":
    main()
