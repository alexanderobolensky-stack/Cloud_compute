"""Periodic sampling of system and per-agent resource usage, plus health checks."""

from __future__ import annotations

import asyncio
import collections
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx
import psutil

from .schemas import AgentSpec
from .supervisor import Supervisor

# Seconds after launch before the first health probe counts.
STARTUP_GRACE = 5


@dataclass
class AgentSample:
    pids: list[int] = field(default_factory=list)
    cpu: float = 0.0
    mem_mb: float = 0.0
    gpu_mem_mb: float = 0.0
    threads: int = 0


@dataclass
class Health:
    ok: bool | None = None
    latency_ms: float | None = None
    checked_at: float | None = None
    error: str | None = None


def query_gpus() -> tuple[list[dict], dict[int, float]]:
    """Return (gpus, {pid: used_mb}) via nvidia-smi; empty when unavailable."""
    if not shutil.which("nvidia-smi"):
        return [], {}
    gpus, per_pid = [], {}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        for line in out.strip().splitlines():
            idx, name, util, used, total, temp = [p.strip() for p in line.split(",")]
            gpus.append({"index": int(idx), "name": name, "util": _num(util),
                         "mem_used_mb": _num(used), "mem_total_mb": _num(total), "temp_c": _num(temp)})
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        for line in out.strip().splitlines():
            pid, used = [p.strip() for p in line.split(",")]
            per_pid[int(pid)] = per_pid.get(int(pid), 0.0) + _num(used)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return gpus, per_pid


def _num(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0


class Monitor:
    def __init__(self, supervisor: Supervisor, history_points: int, interval: float,
                 health_interval: float, emit: Callable[..., None],
                 on_memory_limit: Callable[[str], Awaitable[None]]):
        self.supervisor = supervisor
        self.interval = interval
        self.health_interval = health_interval
        self.emit = emit
        self.on_memory_limit = on_memory_limit
        self.history_points = history_points
        self.system: dict = {}
        self.system_history: collections.deque = collections.deque(maxlen=history_points)
        self.samples: dict[str, AgentSample] = {}
        self.history: dict[str, collections.deque] = {}
        self.health: dict[str, Health] = {}
        self._procs: dict[int, psutil.Process] = {}
        self._net_prev: tuple[float, int, int] | None = None
        self._last_health = 0.0
        self._task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None
        self._http = httpx.AsyncClient(timeout=5.0)
        self._limit_hits: dict[str, float] = {}
        self._has_gpu = shutil.which("nvidia-smi") is not None
        psutil.cpu_percent(None)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        for task in (self._task, self._health_task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self._http.aclose()

    def forget(self, agent_id: str) -> None:
        for store in (self.samples, self.history, self.health, self._limit_hits):
            store.pop(agent_id, None)

    async def _loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.sample()
            except Exception as exc:  # keep sampling no matter what
                self.emit("warning", f"Ошибка сбора метрик: {exc}")
            if time.time() - self._last_health >= self.health_interval and not (
                    self._health_task and not self._health_task.done()):
                self._last_health = time.time()
                self._health_task = asyncio.create_task(self.check_health())
            await asyncio.sleep(max(0.2, self.interval - (time.monotonic() - started)))

    async def sample(self) -> None:
        gpus, gpu_by_pid = await asyncio.to_thread(query_gpus) if self._has_gpu else ([], {})
        specs = [rt.spec for rt in self.supervisor.runtimes.values()]
        result = await asyncio.to_thread(self._collect, specs, gpu_by_pid)
        system, samples = result
        system["gpus"] = gpus
        now = time.time()
        self.system = system
        self.system_history.append((now, system["cpu"], system["mem_percent"],
                                    max((g["util"] for g in gpus), default=None)))
        for spec in specs:
            sample = samples.get(spec.id, AgentSample())
            self.samples[spec.id] = sample
            hist = self.history.setdefault(spec.id, collections.deque(maxlen=self.history_points))
            hist.append((now, round(sample.cpu, 1), round(sample.mem_mb, 1), round(sample.gpu_mem_mb, 1)))
            await self._check_memory(spec, sample)

    def _collect(self, specs: list[AgentSpec], gpu_by_pid: dict[int, float]) -> tuple[dict, dict]:
        vm = psutil.virtual_memory()
        try:
            disk = psutil.disk_usage(str(self.supervisor.logs_dir.resolve().anchor or "/"))
            disk_info = {"disk_percent": disk.percent, "disk_used_gb": disk.used / 1e9,
                         "disk_total_gb": disk.total / 1e9}
        except OSError:
            disk_info = {"disk_percent": None, "disk_used_gb": None, "disk_total_gb": None}
        net = psutil.net_io_counters()
        now = time.time()
        rx = tx = 0.0
        if net and self._net_prev:
            dt = max(0.001, now - self._net_prev[0])
            rx = (net.bytes_recv - self._net_prev[1]) / dt
            tx = (net.bytes_sent - self._net_prev[2]) / dt
        if net:
            self._net_prev = (now, net.bytes_recv, net.bytes_sent)
        try:
            load = list(psutil.getloadavg())
        except (AttributeError, OSError):
            load = None
        system = {
            "ts": now, "cpu": psutil.cpu_percent(None), "cpu_count": psutil.cpu_count(),
            "mem_percent": vm.percent, "mem_used_gb": (vm.total - vm.available) / 1e9,
            "mem_total_gb": vm.total / 1e9, "load": load, "net_rx": rx, "net_tx": tx,
            "boot_time": psutil.boot_time(), **disk_info,
        }

        roots: dict[str, list[int]] = {}
        matchers: dict[str, re.Pattern] = {}
        for spec in specs:
            rt = self.supervisor.runtimes.get(spec.id)
            if spec.kind == "process" and rt and rt.pid:
                roots[spec.id] = [rt.pid]
            elif spec.kind == "external" and spec.process_match:
                try:
                    matchers[spec.id] = re.compile(spec.process_match)
                except re.error:
                    pass
        if matchers:
            own = psutil.Process().pid
            for proc in psutil.process_iter(["pid", "cmdline"]):
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if not cmdline or proc.info["pid"] == own:
                    continue
                for agent_id, pattern in matchers.items():
                    if pattern.search(cmdline):
                        roots.setdefault(agent_id, []).append(proc.info["pid"])

        samples: dict[str, AgentSample] = {}
        seen: set[int] = set()
        for agent_id, pids in roots.items():
            sample = AgentSample(pids=sorted(pids))
            tree: set[int] = set()
            for pid in pids:
                tree.add(pid)
                try:
                    tree.update(c.pid for c in psutil.Process(pid).children(recursive=True))
                except psutil.Error:
                    pass
            for pid in tree:
                proc = self._process(pid)
                if proc is None:
                    continue
                seen.add(pid)
                try:
                    with proc.oneshot():
                        sample.cpu += proc.cpu_percent(None)
                        sample.mem_mb += proc.memory_info().rss / 2**20
                        sample.threads += proc.num_threads()
                except psutil.Error:
                    continue
                sample.gpu_mem_mb += gpu_by_pid.get(pid, 0.0)
            samples[agent_id] = sample
        for pid in list(self._procs):
            if pid not in seen:
                self._procs.pop(pid, None)
        return system, samples

    def _process(self, pid: int) -> psutil.Process | None:
        proc = self._procs.get(pid)
        if proc is not None and proc.is_running():
            return proc
        try:
            proc = psutil.Process(pid)
            proc.cpu_percent(None)  # prime the counter; the first real value comes next tick
        except psutil.Error:
            return None
        self._procs[pid] = proc
        return proc

    async def _check_memory(self, spec: AgentSpec, sample: AgentSample) -> None:
        if not spec.max_memory_mb or sample.mem_mb <= spec.max_memory_mb:
            return
        # Avoid restart storms: act at most once per minute per agent.
        if time.time() - self._limit_hits.get(spec.id, 0) < 60:
            return
        self._limit_hits[spec.id] = time.time()
        self.emit("error", f"Превышен лимит памяти: {sample.mem_mb:.0f} МБ > {spec.max_memory_mb} МБ",
                  spec.id)
        if spec.kind == "process":
            await self.on_memory_limit(spec.id)

    async def check_health(self) -> None:
        specs = [rt.spec for rt in self.supervisor.runtimes.values() if rt.spec.health_url]
        await asyncio.gather(*(self._probe(spec) for spec in specs), return_exceptions=True)

    async def _probe(self, spec: AgentSpec) -> None:
        rt = self.supervisor.runtimes.get(spec.id)
        if spec.kind == "process" and (rt is None or rt.state != "running"):
            self.health.pop(spec.id, None)
            return
        if spec.kind == "process" and rt.started_at and time.time() - rt.started_at < STARTUP_GRACE:
            return  # give the agent time to open its port
        prev = self.health.get(spec.id, Health())
        started = time.perf_counter()
        health = Health(checked_at=time.time())
        try:
            resp = await self._http.get(spec.health_url)
            health.latency_ms = round((time.perf_counter() - started) * 1000, 1)
            health.ok = resp.status_code < 400
            if not health.ok:
                health.error = f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            health.ok = False
            health.error = type(exc).__name__
        self.health[spec.id] = health
        if prev.ok is not None and prev.ok != health.ok:
            if health.ok:
                self.emit("info", "Проверка здоровья снова проходит", spec.id)
            else:
                self.emit("warning", f"Проверка здоровья не проходит: {health.error}", spec.id)
        elif prev.ok is None and health.ok is False:
            self.emit("warning", f"Проверка здоровья не проходит: {health.error}", spec.id)
