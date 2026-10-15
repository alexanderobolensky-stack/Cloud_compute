"""Runtime settings, read from environment variables (overridable from the CLI)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _split(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8700
    config_file: Path = Path("agents.yaml")
    data_dir: Path = Path("data")
    # When set, every API/WebSocket request must carry this token.
    token: str | None = None
    # Extra Host header values accepted besides localhost (DNS-rebinding guard).
    allowed_hosts: list[str] = field(default_factory=list)
    sample_interval: float = 2.0
    health_interval: float = 10.0
    history_seconds: int = 3600
    log_lines: int = 2000
    stop_timeout: float = 10.0
    # Optional URL that receives a JSON POST for every warning/error event.
    webhook_url: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            host=env.get("PORTAL_HOST", "127.0.0.1"),
            port=int(env.get("PORTAL_PORT", "8700")),
            config_file=Path(env.get("PORTAL_CONFIG", "agents.yaml")),
            data_dir=Path(env.get("PORTAL_DATA", "data")),
            token=env.get("PORTAL_TOKEN") or None,
            allowed_hosts=_split(env.get("PORTAL_ALLOWED_HOSTS")),
            sample_interval=float(env.get("PORTAL_SAMPLE_INTERVAL", "2")),
            health_interval=float(env.get("PORTAL_HEALTH_INTERVAL", "10")),
            history_seconds=int(env.get("PORTAL_HISTORY_SECONDS", "3600")),
            webhook_url=env.get("PORTAL_WEBHOOK_URL") or None,
        )

    @property
    def history_points(self) -> int:
        return max(10, int(self.history_seconds / self.sample_interval))

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"
