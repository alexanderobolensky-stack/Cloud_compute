"""Status and quick prompts for local model servers (Ollama, OpenAI-compatible)."""

from __future__ import annotations

import time

import httpx

from .schemas import BackendSpec


async def backend_status(client: httpx.AsyncClient, spec: BackendSpec) -> dict:
    base = spec.url.rstrip("/")
    result = {"name": spec.name, "type": spec.type, "url": spec.url, "online": False,
              "version": None, "models": [], "running": [], "error": None, "latency_ms": None}
    started = time.perf_counter()
    try:
        if spec.type == "ollama":
            tags = await client.get(f"{base}/api/tags")
            tags.raise_for_status()
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            result["models"] = [
                {"name": m.get("name"), "size_gb": round((m.get("size") or 0) / 1e9, 2),
                 "family": (m.get("details") or {}).get("family"),
                 "parameters": (m.get("details") or {}).get("parameter_size"),
                 "quantization": (m.get("details") or {}).get("quantization_level"),
                 "modified_at": m.get("modified_at")}
                for m in tags.json().get("models", [])
            ]
            try:
                ps = await client.get(f"{base}/api/ps")
                result["running"] = [
                    {"name": m.get("name"), "vram_gb": round((m.get("size_vram") or 0) / 1e9, 2),
                     "size_gb": round((m.get("size") or 0) / 1e9, 2), "expires_at": m.get("expires_at")}
                    for m in ps.json().get("models", [])
                ]
                version = await client.get(f"{base}/api/version")
                result["version"] = version.json().get("version")
            except (httpx.HTTPError, ValueError):
                pass
        else:
            resp = await client.get(f"{base}/models")
            resp.raise_for_status()
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            result["models"] = [{"name": m.get("id"), "owned_by": m.get("owned_by")}
                                for m in resp.json().get("data", [])]
        result["online"] = True
    except (httpx.HTTPError, ValueError) as exc:
        result["error"] = str(exc) or type(exc).__name__
    return result


async def run_prompt(client: httpx.AsyncClient, spec: BackendSpec, model: str, prompt: str) -> dict:
    base = spec.url.rstrip("/")
    started = time.perf_counter()
    if spec.type == "ollama":
        resp = await client.post(f"{base}/api/generate",
                                 json={"model": model, "prompt": prompt, "stream": False},
                                 timeout=300)
        resp.raise_for_status()
        data = resp.json()
        text = data.get("response", "")
        tokens = data.get("eval_count")
        eval_s = (data.get("eval_duration") or 0) / 1e9
    else:
        resp = await client.post(f"{base}/chat/completions",
                                 json={"model": model, "messages": [{"role": "user", "content": prompt}]},
                                 timeout=300)
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        tokens = (data.get("usage") or {}).get("completion_tokens")
        eval_s = 0
    elapsed = time.perf_counter() - started
    return {
        "text": text,
        "elapsed_s": round(elapsed, 2),
        "tokens": tokens,
        "tokens_per_s": round(tokens / (eval_s or elapsed), 1) if tokens else None,
    }
