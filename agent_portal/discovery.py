"""Finds running processes that look like local AI runtimes or agent frameworks."""

from __future__ import annotations

import re

import psutil

# (label, pattern matched against the command line)
SIGNATURES: list[tuple[str, str]] = [
    ("Ollama", r"\bollama\b"),
    ("llama.cpp", r"llama-server|llama-cli|llama\.cpp|llamafile"),
    ("vLLM", r"\bvllm\b"),
    ("LM Studio", r"lm[-_ ]?studio|\blms\b"),
    ("LocalAI", r"\blocal-ai\b|\blocalai\b"),
    ("KoboldCpp", r"koboldcpp"),
    ("text-generation-webui", r"text-generation-webui|server\.py.*--model"),
    ("GPT4All", r"gpt4all"),
    ("Jan", r"\bjan\b.*(nitro|cortex)|cortex-cpp"),
    ("ComfyUI", r"comfyui|ComfyUI"),
    ("Open WebUI", r"open[-_]webui"),
    ("TGI", r"text-generation-launcher|text-generation-router"),
    ("CrewAI", r"\bcrewai\b"),
    ("AutoGen", r"\bautogen\b"),
    ("LangGraph", r"langgraph"),
    ("LangChain", r"langchain"),
    ("OpenHands", r"openhands"),
    ("Aider", r"\baider\b"),
    ("MCP server", r"mcp[-_]server|modelcontextprotocol"),
]
_COMPILED = [(label, re.compile(pattern, re.IGNORECASE)) for label, pattern in SIGNATURES]


def discover(known_pids: set[int]) -> list[dict]:
    own = psutil.Process().pid
    found = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time", "memory_info", "username"]):
        info = proc.info
        if info["pid"] == own:
            continue
        cmdline = " ".join(info.get("cmdline") or []) or (info.get("name") or "")
        label = next((lbl for lbl, rx in _COMPILED if rx.search(cmdline)), None)
        if not label:
            continue
        mem = info.get("memory_info")
        found.append({
            "pid": info["pid"],
            "name": info.get("name") or "",
            "label": label,
            "cmdline": cmdline[:500],
            "user": info.get("username"),
            "started_at": info.get("create_time"),
            "mem_mb": round(mem.rss / 2**20, 1) if mem else None,
            "managed": info["pid"] in known_pids,
            "suggested_match": suggest_match(info),
        })
    found.sort(key=lambda item: (item["managed"], item["label"], item["pid"]))
    return found


def suggest_match(info: dict) -> str:
    """A regex that would re-find this process after a restart (its executable + script)."""
    parts = info.get("cmdline") or [info.get("name") or ""]
    exe = parts[0].replace("\\", "/").rsplit("/", 1)[-1]
    script = next((p for p in parts[1:] if not p.startswith("-")), None)
    # Anchor to the executable so unrelated commands that merely mention it don't match.
    prefix = r"^\S*" + re.escape(exe) + r"(\s|$)"
    if script and len(parts) > 1:
        script = script.replace("\\", "/").rsplit("/", 1)[-1]
        return prefix + r".*" + re.escape(script)
    return prefix
