"""FastAPI application: REST API, live WebSockets and the static UI."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .backends import backend_status, run_prompt
from .config import Settings
from .discovery import discover
from .monitor import Monitor
from .schemas import AgentSpec, BackendSpec, PromptRequest, TaskRequest
from .security import GuardMiddleware
from .store import ConfigStore, EventLog
from .supervisor import Supervisor

STATIC = Path(__file__).parent / "static"


class Portal:
    """Wires the store, supervisor and monitor together."""

    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = ConfigStore(settings.config_file)
        self.events = EventLog(settings.data_dir / "portal.db")
        self.supervisor = Supervisor(settings.logs_dir, settings.log_lines,
                                     settings.stop_timeout, self.emit)
        self.monitor = Monitor(self.supervisor, settings.history_points, settings.sample_interval,
                               settings.health_interval, self.emit, self._memory_restart)
        self.http = httpx.AsyncClient(timeout=10.0)
        self.event_subscribers: set[asyncio.Queue] = set()
        self._background: set[asyncio.Task] = set()

    def emit(self, level: str, message: str, agent_id: str | None = None) -> None:
        event = self.events.add(level, message, agent_id)
        for queue in list(self.event_subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
        if self.settings.webhook_url and level in ("warning", "error"):
            self._spawn(self._notify(event))

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _notify(self, event: dict) -> None:
        with contextlib.suppress(httpx.HTTPError):
            await self.http.post(self.settings.webhook_url, json={"source": "agent-portal", **event})

    async def _memory_restart(self, agent_id: str) -> None:
        self._spawn(self.supervisor.restart(agent_id))

    async def startup(self) -> None:
        for spec in self.store.agents:
            self.supervisor.register(spec)
        self.emit("info", f"Портал запущен, агентов: {len(self.store.agents)}")
        for spec in self.store.agents:
            if spec.kind == "process" and spec.autostart:
                await self.supervisor.start(spec.id)
        self.monitor.start()

    async def shutdown(self) -> None:
        await self.monitor.close()
        await self.supervisor.shutdown()
        await self.http.aclose()
        self.events.close()

    def agent_status(self, agent_id: str) -> dict:
        rt = self.supervisor.runtimes[agent_id]
        spec = rt.spec
        sample = self.monitor.samples.get(agent_id)
        health = self.monitor.health.get(agent_id)
        if spec.kind == "external":
            # Either signal is enough: the process was found, or its health endpoint answers.
            if (sample and sample.pids) or (health and health.ok):
                state = "running"
            elif health is None and not (sample and spec.process_match):
                state = "unknown"
            else:
                state = "missing"
        else:
            state = rt.state
        pids = sample.pids if sample else ([rt.pid] if rt.pid else [])
        started_at = rt.started_at
        return {
            "id": spec.id, "name": spec.name, "description": spec.description, "kind": spec.kind,
            "tags": spec.tags, "state": state, "pid": rt.pid, "pids": pids,
            "uptime": round(time.time() - started_at) if started_at else None,
            "restarts": rt.restarts, "exit_code": rt.exit_code,
            "cpu": round(sample.cpu, 1) if sample else 0.0,
            "mem_mb": round(sample.mem_mb, 1) if sample else 0.0,
            "gpu_mem_mb": round(sample.gpu_mem_mb, 1) if sample else 0.0,
            "threads": sample.threads if sample else 0,
            "max_memory_mb": spec.max_memory_mb,
            "health": None if health is None else {
                "ok": health.ok, "latency_ms": health.latency_ms,
                "checked_at": health.checked_at, "error": health.error},
            "has_health": bool(spec.health_url), "has_task": bool(spec.task_url),
        }

    def snapshot(self) -> dict:
        return {
            "ts": time.time(),
            "system": self.monitor.system,
            "agents": [self.agent_status(aid) for aid in self.supervisor.runtimes],
        }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    portal = Portal(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await portal.startup()
        try:
            yield
        finally:
            await portal.shutdown()

    app = FastAPI(title="Портал локальных ИИ-агентов", version=__version__, lifespan=lifespan)
    app.state.portal = portal
    app.add_middleware(GuardMiddleware, token=settings.token, allowed_hosts=settings.allowed_hosts)

    def runtime_or_404(agent_id: str):
        rt = portal.supervisor.runtimes.get(agent_id)
        if rt is None:
            raise HTTPException(404, "Агент не найден")
        return rt

    # meta -------------------------------------------------------------------

    @app.get("/api/info")
    async def info():
        return {"version": __version__, "auth": bool(settings.token),
                "sample_interval": settings.sample_interval,
                "history_seconds": settings.history_seconds,
                "config_file": str(settings.config_file)}

    @app.post("/api/auth")
    async def auth(request: Request, response: Response):
        body = await request.json()
        token = str(body.get("token", ""))
        if not settings.token or not hmac.compare_digest(token.encode(), settings.token.encode()):
            raise HTTPException(401, "Неверный токен")
        response.set_cookie("portal_token", token, httponly=True, samesite="strict")
        return {"ok": True}

    @app.get("/api/overview")
    async def overview():
        return portal.snapshot()

    @app.get("/api/system/history")
    async def system_history():
        return {"points": list(portal.monitor.system_history)}

    # agents -----------------------------------------------------------------

    @app.get("/api/agents")
    async def list_agents():
        return [portal.agent_status(aid) for aid in portal.supervisor.runtimes]

    @app.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str):
        rt = runtime_or_404(agent_id)
        return {**portal.agent_status(agent_id), "spec": rt.spec.model_dump(mode="json")}

    @app.post("/api/agents", status_code=201)
    async def create_agent(spec: AgentSpec):
        if spec.id in portal.supervisor.runtimes:
            raise HTTPException(409, "Агент с таким id уже существует")
        portal.store.upsert(spec)
        portal.supervisor.register(spec)
        portal.emit("info", "Агент добавлен", spec.id)
        if spec.kind == "process" and spec.autostart:
            await portal.supervisor.start(spec.id)
        return portal.agent_status(spec.id)

    @app.put("/api/agents/{agent_id}")
    async def update_agent(agent_id: str, spec: AgentSpec):
        rt = runtime_or_404(agent_id)
        if spec.id != agent_id:
            raise HTTPException(400, "id агента нельзя изменить")
        was_running = rt.state in ("running", "starting", "backoff")
        if was_running and rt.spec.kind == "process":
            await portal.supervisor.stop(agent_id)
        portal.store.upsert(spec)
        portal.supervisor.register(spec)
        portal.monitor.forget(agent_id)
        portal.emit("info", "Конфигурация обновлена", agent_id)
        if was_running and spec.kind == "process":
            await portal.supervisor.start(agent_id)
        return portal.agent_status(agent_id)

    @app.delete("/api/agents/{agent_id}", status_code=204)
    async def delete_agent(agent_id: str):
        runtime_or_404(agent_id)
        await portal.supervisor.unregister(agent_id)
        portal.store.delete(agent_id)
        portal.monitor.forget(agent_id)
        portal.emit("info", "Агент удалён", agent_id)

    async def agent_action(agent_id: str, action: str):
        rt = runtime_or_404(agent_id)
        if rt.spec.kind != "process":
            raise HTTPException(400, "Внешними агентами портал не управляет")
        await getattr(portal.supervisor, action)(agent_id)
        return portal.agent_status(agent_id)

    @app.post("/api/agents/{agent_id}/start")
    async def start_agent(agent_id: str):
        return await agent_action(agent_id, "start")

    @app.post("/api/agents/{agent_id}/stop")
    async def stop_agent(agent_id: str):
        return await agent_action(agent_id, "stop")

    @app.post("/api/agents/{agent_id}/restart")
    async def restart_agent(agent_id: str):
        return await agent_action(agent_id, "restart")

    @app.get("/api/agents/{agent_id}/metrics")
    async def agent_metrics(agent_id: str):
        runtime_or_404(agent_id)
        return {"points": list(portal.monitor.history.get(agent_id, []))}

    @app.get("/api/agents/{agent_id}/logs")
    async def agent_logs(agent_id: str, tail: int = 500):
        rt = runtime_or_404(agent_id)
        lines = list(rt.logs)[-max(1, min(tail, settings.log_lines)):]
        return {"lines": [{"ts": ts, "text": text} for ts, text in lines]}

    @app.delete("/api/agents/{agent_id}/logs", status_code=204)
    async def clear_logs(agent_id: str):
        runtime_or_404(agent_id)
        portal.supervisor.clear_logs(agent_id)

    @app.post("/api/agents/{agent_id}/task")
    async def send_task(agent_id: str, task: TaskRequest):
        rt = runtime_or_404(agent_id)
        if not rt.spec.task_url:
            raise HTTPException(400, "У агента не задан task_url")
        started = time.perf_counter()
        try:
            resp = await portal.http.post(rt.spec.task_url, json={"input": task.input}, timeout=300)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Агент недоступен: {type(exc).__name__}") from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text
        portal.emit("info", "Задача отправлена" if resp.is_success else f"Задача отклонена (HTTP {resp.status_code})",
                    agent_id)
        return {"status": resp.status_code, "elapsed_s": round(time.perf_counter() - started, 2),
                "response": payload}

    # events, discovery, backends -------------------------------------------

    @app.get("/api/events")
    async def list_events(agent_id: str | None = None, level: str | None = None, limit: int = 200):
        return portal.events.list(agent_id=agent_id, level=level, limit=limit)

    @app.get("/api/discovery")
    async def discovery():
        known: set[int] = set()
        for sample in portal.monitor.samples.values():
            known.update(sample.pids)
        return await asyncio.to_thread(discover, known)

    @app.get("/api/backends")
    async def list_backends():
        return await asyncio.gather(*(backend_status(portal.http, b) for b in portal.store.backends))

    @app.put("/api/backends")
    async def save_backends(backends: list[BackendSpec]):
        portal.store.set_backends(backends)
        portal.emit("info", "Список серверов моделей обновлён")
        return await list_backends()

    @app.post("/api/backends/{index}/prompt")
    async def prompt_backend(index: int, req: PromptRequest):
        backends = portal.store.backends
        if not 0 <= index < len(backends):
            raise HTTPException(404, "Сервер моделей не найден")
        try:
            return await run_prompt(portal.http, backends[index], req.model, req.prompt)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise HTTPException(502, f"Ошибка сервера моделей: {exc}") from exc

    # live -------------------------------------------------------------------

    @app.websocket("/ws/live")
    async def live(ws: WebSocket):
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        portal.event_subscribers.add(queue)
        try:
            while True:
                await ws.send_json({"type": "snapshot", **portal.snapshot()})
                deadline = time.monotonic() + settings.sample_interval
                while (remaining := deadline - time.monotonic()) > 0:
                    try:
                        event = await asyncio.wait_for(queue.get(), remaining)
                    except asyncio.TimeoutError:
                        break
                    await ws.send_json({"type": "event", "event": event})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            portal.event_subscribers.discard(queue)

    @app.websocket("/ws/agents/{agent_id}/logs")
    async def live_logs(ws: WebSocket, agent_id: str):
        if agent_id not in portal.supervisor.runtimes:
            await ws.close(code=4404)
            return
        await ws.accept()
        queue = portal.supervisor.subscribe(agent_id)
        try:
            while True:
                ts, text = await queue.get()
                await ws.send_json({"ts": ts, "text": text})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            portal.supervisor.unsubscribe(agent_id, queue)

    # UI ---------------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
