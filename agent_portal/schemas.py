"""Data models shared by the store, the supervisor and the API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

AgentKind = Literal["process", "external"]
RestartPolicy = Literal["never", "on-failure", "always"]
BackendType = Literal["ollama", "openai"]


class AgentSpec(BaseModel):
    """Declarative description of an agent, as stored in agents.yaml."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    # process: the portal launches and supervises the command.
    # external: the agent runs on its own; the portal only observes it.
    kind: AgentKind = "process"
    command: str | list[str] | None = None
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    autostart: bool = False
    restart_policy: RestartPolicy = "on-failure"
    max_restarts: int = Field(default=5, ge=0, le=1000)
    max_memory_mb: int | None = Field(default=None, ge=1)
    # Regex matched against the full command line of running processes.
    process_match: str | None = None
    health_url: str | None = None
    # Endpoint accepting POST {"input": "..."}; powers the task console.
    task_url: str | None = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_kind(self) -> "AgentSpec":
        if self.kind == "process" and not self.command:
            raise ValueError("укажите команду запуска агента — или выберите «Только наблюдать»")
        if self.kind == "external" and not (self.process_match or self.health_url):
            raise ValueError("укажите шаблон командной строки или адрес проверки здоровья")
        return self


class BackendSpec(BaseModel):
    """A local model server (Ollama or any OpenAI-compatible API)."""

    name: str = Field(min_length=1, max_length=120)
    type: BackendType = "ollama"
    url: str


class PortalConfig(BaseModel):
    backends: list[BackendSpec] = Field(default_factory=list)
    agents: list[AgentSpec] = Field(default_factory=list)


class TaskRequest(BaseModel):
    input: str = Field(min_length=1, max_length=100_000)


class PromptRequest(BaseModel):
    model: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=100_000)
