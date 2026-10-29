from dataclasses import dataclass, field
from enum import StrEnum


class Approach(StrEnum):
    AZURE_OPENAI = "azure-openai"
    FOUNDRY = "foundry"
    APIM = "apim"
    AZURE_ML = "azure-ml"


class ModelServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Target:
    endpoint: str
    credential: str = field(repr=False)
    deployment: str = "tenant-chat"


@dataclass(frozen=True)
class Customer:
    tenant_id: str
    caller_token: str = field(repr=False)
    target: Target


@dataclass(frozen=True)
class Request:
    tenant_id: str
    agent_id: str
    session_id: str
    model_alias: str
    prompt: str = field(repr=False)


@dataclass(frozen=True)
class Result:
    content: str = field(repr=False)
    worker_pid: int
    workspace_id: str
    turn: int
