"""Command-line entry point: ``python -m agent_portal``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .config import Settings
from .security import LOCAL_HOSTS


def main(argv: list[str] | None = None) -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="Портал управления и мониторинга локальных ИИ-агентов")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--config", type=Path, default=settings.config_file, help="путь к agents.yaml")
    parser.add_argument("--data", type=Path, default=settings.data_dir, help="каталог для логов и БД")
    args = parser.parse_args(argv)
    settings.host, settings.port = args.host, args.port
    settings.config_file, settings.data_dir = args.config, args.data

    if settings.host not in LOCAL_HOSTS and not settings.token:
        sys.exit("Отказ: при привязке не к localhost задайте PORTAL_TOKEN — портал умеет запускать команды.")
    if settings.host not in LOCAL_HOSTS and not settings.allowed_hosts:
        # The token already protects the API; accept whatever name the LAN uses.
        settings.allowed_hosts.append("*")

    print(f"Портал агентов: http://{'localhost' if settings.host in LOCAL_HOSTS else settings.host}:{settings.port}")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
