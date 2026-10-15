"""Persistence: agent definitions live in a YAML file, events in SQLite."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import yaml

from .schemas import AgentSpec, BackendSpec, PortalConfig

HEADER = (
    "# Local AI agent portal configuration.\n"
    "# Edited by the portal UI; manual edits are picked up on restart.\n"
)


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.config = self._load()

    def _load(self) -> PortalConfig:
        if not self.path.exists():
            return PortalConfig()
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return PortalConfig.model_validate(raw)

    def _save(self) -> None:
        data = self.config.model_dump(mode="json", exclude_defaults=False)
        text = HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".agents-", suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, self.path)

    @property
    def agents(self) -> list[AgentSpec]:
        return list(self.config.agents)

    @property
    def backends(self) -> list[BackendSpec]:
        return list(self.config.backends)

    def get(self, agent_id: str) -> AgentSpec | None:
        return next((a for a in self.config.agents if a.id == agent_id), None)

    def upsert(self, spec: AgentSpec) -> None:
        with self._lock:
            agents = [a for a in self.config.agents if a.id != spec.id]
            index = next((i for i, a in enumerate(self.config.agents) if a.id == spec.id), len(agents))
            agents.insert(index, spec)
            self.config.agents = agents
            self._save()

    def delete(self, agent_id: str) -> bool:
        with self._lock:
            before = len(self.config.agents)
            self.config.agents = [a for a in self.config.agents if a.id != agent_id]
            if len(self.config.agents) == before:
                return False
            self._save()
            return True

    def set_backends(self, backends: list[BackendSpec]) -> None:
        with self._lock:
            self.config.backends = backends
            self._save()


class EventLog:
    """Append-only log of lifecycle events (start, crash, health changes...)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts REAL NOT NULL, agent_id TEXT, level TEXT NOT NULL,"
            " message TEXT NOT NULL, data TEXT)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS events_agent ON events(agent_id, id)")
        self._db.commit()

    def add(self, level: str, message: str, agent_id: str | None = None, data: dict | None = None) -> dict:
        ts = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events (ts, agent_id, level, message, data) VALUES (?, ?, ?, ?, ?)",
                (ts, agent_id, level, message, json.dumps(data) if data else None),
            )
            self._db.commit()
        return {"id": cur.lastrowid, "ts": ts, "agent_id": agent_id, "level": level,
                "message": message, "data": data}

    def list(self, agent_id: str | None = None, level: str | None = None, limit: int = 200) -> list[dict]:
        query = "SELECT id, ts, agent_id, level, message, data FROM events"
        clauses, params = [], []
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if level:
            clauses.append("level = ?")
            params.append(level)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [
            {"id": r[0], "ts": r[1], "agent_id": r[2], "level": r[3], "message": r[4],
             "data": json.loads(r[5]) if r[5] else None}
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._db.close()
