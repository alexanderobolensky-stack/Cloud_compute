"""Launches agent processes, captures their output and applies restart policies."""

from __future__ import annotations

import asyncio
import collections
import os
import shlex
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .schemas import AgentSpec

# Restart counter resets once a process has stayed up this long.
STABLE_SECONDS = 60
MAX_BACKOFF = 30
LOG_ROTATE_BYTES = 10 * 1024 * 1024
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


@dataclass
class Runtime:
    spec: AgentSpec
    state: str = "stopped"  # stopped|starting|running|stopping|backoff|crashed|exited
    pid: int | None = None
    started_at: float | None = None
    exit_code: int | None = None
    restarts: int = 0
    process: asyncio.subprocess.Process | None = None
    logs: collections.deque = field(default_factory=lambda: collections.deque(maxlen=2000))
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    watcher: asyncio.Task | None = None
    retry: asyncio.Task | None = None
    stop_requested: bool = False
    lines_written: int = 0


def build_command(spec: AgentSpec) -> list[str]:
    raw = spec.command or []
    parts = shlex.split(raw, posix=os.name == "posix") if isinstance(raw, str) else list(raw)
    # "{python}" runs scripts with the portal's own interpreter; "{examples}" points
    # at the bundled example agents wherever the portal was started from.
    return [p.replace("{python}", sys.executable).replace("{examples}", str(EXAMPLES_DIR)) for p in parts]


class Supervisor:
    def __init__(self, logs_dir: Path, log_lines: int, stop_timeout: float,
                 emit: Callable[..., None]):
        self.logs_dir = logs_dir
        self.log_lines = log_lines
        self.stop_timeout = stop_timeout
        self.emit = emit  # emit(level, message, agent_id)
        self.runtimes: dict[str, Runtime] = {}
        logs_dir.mkdir(parents=True, exist_ok=True)

    # registry -----------------------------------------------------------

    def register(self, spec: AgentSpec) -> Runtime:
        rt = self.runtimes.get(spec.id)
        if rt is None:
            rt = Runtime(spec=spec, logs=collections.deque(maxlen=self.log_lines))
            self.runtimes[spec.id] = rt
            self._load_tail(rt)
        else:
            rt.spec = spec
        return rt

    async def unregister(self, agent_id: str) -> None:
        rt = self.runtimes.get(agent_id)
        if rt:
            await self.stop(agent_id)
            self.runtimes.pop(agent_id, None)

    def _load_tail(self, rt: Runtime) -> None:
        path = self.log_path(rt.spec.id)
        if not path.exists():
            return
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 256 * 1024))
            lines = fh.read().decode("utf-8", "replace").splitlines()[-self.log_lines:]
        for line in lines:
            rt.logs.append((0.0, line))

    def log_path(self, agent_id: str) -> Path:
        return self.logs_dir / f"{agent_id}.log"

    # lifecycle ----------------------------------------------------------

    async def start(self, agent_id: str, *, reset_restarts: bool = True) -> Runtime:
        rt = self.runtimes[agent_id]
        if rt.spec.kind != "process":
            raise ValueError("external agents are not managed by the portal")
        if rt.state in ("starting", "running", "stopping"):
            return rt
        if rt.retry:
            rt.retry.cancel()
            rt.retry = None
        if reset_restarts:
            rt.restarts = 0
        rt.stop_requested = False
        rt.state = "starting"
        rt.exit_code = None
        cmd = build_command(rt.spec)
        env = {**os.environ, "PYTHONUNBUFFERED": "1", **rt.spec.env}
        kwargs: dict = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True  # own process group -> clean group kill
        else:
            kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        self._log(rt, f"[portal] starting: {shlex.join(cmd)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=rt.spec.cwd or None, env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **kwargs,
            )
        except (OSError, ValueError) as exc:
            rt.state = "crashed"
            self._log(rt, f"[portal] failed to start: {exc}")
            self.emit("error", f"Не удалось запустить: {exc}", agent_id)
            return rt
        rt.process = proc
        rt.pid = proc.pid
        rt.started_at = time.time()
        rt.state = "running"
        self.emit("info", f"Запущен (PID {proc.pid})", agent_id)
        rt.watcher = asyncio.create_task(self._watch(rt, proc))
        return rt

    async def stop(self, agent_id: str) -> Runtime:
        rt = self.runtimes[agent_id]
        rt.stop_requested = True
        if rt.retry:
            rt.retry.cancel()
            rt.retry = None
            if rt.state == "backoff":
                rt.state = "stopped"
        proc = rt.process
        if proc is None or proc.returncode is not None:
            if rt.state not in ("crashed", "exited"):
                rt.state = "stopped"
            return rt
        rt.state = "stopping"
        self._log(rt, "[portal] stopping")
        self._signal(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), self.stop_timeout)
        except asyncio.TimeoutError:
            self._log(rt, "[portal] did not exit in time, killing")
            self._signal(proc, signal.SIGKILL if os.name == "posix" else signal.SIGTERM, force=True)
            await proc.wait()
        if rt.watcher:
            await asyncio.gather(rt.watcher, return_exceptions=True)
        return rt

    async def restart(self, agent_id: str) -> Runtime:
        await self.stop(agent_id)
        return await self.start(agent_id)

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(self.stop(aid) for aid, rt in self.runtimes.items() if rt.process),
            return_exceptions=True,
        )

    def _signal(self, proc: asyncio.subprocess.Process, sig: int, force: bool = False) -> None:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), sig)
            elif force:
                proc.kill()
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError):
            pass

    async def _watch(self, rt: Runtime, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            self._log(rt, line.decode("utf-8", "replace").rstrip("\r\n"))
        code = await proc.wait()
        uptime = time.time() - (rt.started_at or time.time())
        rt.exit_code = code
        rt.process = None
        rt.pid = None
        rt.started_at = None
        self._log(rt, f"[portal] exited with code {code}")
        if rt.stop_requested:
            rt.state = "stopped"
            self.emit("info", "Остановлен", rt.spec.id)
            return
        failed = code != 0
        rt.state = "crashed" if failed else "exited"
        self.emit("error" if failed else "warning",
                  f"Процесс завершился с кодом {code}", rt.spec.id)
        policy = rt.spec.restart_policy
        if policy == "never" or (policy == "on-failure" and not failed):
            return
        if uptime >= STABLE_SECONDS:
            rt.restarts = 0
        if rt.restarts >= rt.spec.max_restarts:
            self.emit("error", f"Достигнут лимит перезапусков ({rt.spec.max_restarts})", rt.spec.id)
            return
        delay = min(MAX_BACKOFF, 2 ** rt.restarts)
        rt.restarts += 1
        rt.state = "backoff"
        self._log(rt, f"[portal] restarting in {delay}s (attempt {rt.restarts})")
        rt.retry = asyncio.create_task(self._delayed_start(rt, delay))

    async def _delayed_start(self, rt: Runtime, delay: float) -> None:
        await asyncio.sleep(delay)
        rt.retry = None
        if not rt.stop_requested and rt.spec.id in self.runtimes:
            rt.state = "stopped"
            await self.start(rt.spec.id, reset_restarts=False)

    # logs ---------------------------------------------------------------

    def _log(self, rt: Runtime, line: str) -> None:
        entry = (time.time(), line)
        rt.logs.append(entry)
        path = self.log_path(rt.spec.id)
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            rt.lines_written += 1
            if rt.lines_written % 1000 == 0 and path.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(path, path.with_suffix(".log.1"))
        except OSError:
            pass
        for queue in list(rt.subscribers):
            try:
                queue.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def subscribe(self, agent_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.runtimes[agent_id].subscribers.add(queue)
        return queue

    def unsubscribe(self, agent_id: str, queue: asyncio.Queue) -> None:
        rt = self.runtimes.get(agent_id)
        if rt:
            rt.subscribers.discard(queue)

    def clear_logs(self, agent_id: str) -> None:
        rt = self.runtimes[agent_id]
        rt.logs.clear()
        self.log_path(agent_id).write_text("", encoding="utf-8")
