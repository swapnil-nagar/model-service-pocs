import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings
from .service import ChatMessage, LivePocError, ModelReply, SessionKey

SYSTEM_INSTRUCTIONS = (
    "You are a model-service isolation POC. Use only this conversation's context. "
    "Reply concisely in at most two sentences. You have no tools or access to files."
)


class _Message(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    content: str


class _Choice(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    message: _Message


class _PromptDetails(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    cached_tokens: int | None = Field(default=None, ge=0)


class _Usage(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    prompt_tokens_details: _PromptDetails | None = None


class _Completion(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    choices: list[_Choice] = Field(min_length=1)
    model: str | None = None
    usage: _Usage | None = None


class FoundryBackend:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20), follow_redirects=False, trust_env=False
        )

    async def infer(
        self,
        scope: SessionKey,
        prompt: str,
        history: tuple[ChatMessage, ...],
        invocation_id: str,
    ) -> ModelReply:
        keys = {
            "customer-a": self.settings.tenant_a_key,
            "customer-b": self.settings.tenant_b_key,
        }
        key = keys.get(scope.tenant_id)
        if not key:
            raise LivePocError("tenant_credential_unavailable", 503)
        messages = [
            {"role": "system", "content": SYSTEM_INSTRUCTIONS},
            *({"role": message.role, "content": message.content} for message in history),
            {"role": "user", "content": prompt},
        ]
        try:
            response = await self.client.post(
                self.settings.foundry_gateway_base_url + "/chat/completions",
                headers={
                    "Ocp-Apim-Subscription-Key": key,
                    "X-Poc-Tenant-Id": scope.tenant_id,
                    "X-Poc-Invocation-Id": invocation_id,
                },
                json={"model": "chat", "messages": messages, "max_tokens": 128, "store": False},
            )
        except httpx.TimeoutException:
            raise LivePocError("gateway_timeout", 504) from None
        except httpx.RequestError:
            raise LivePocError("gateway_unavailable", 503) from None
        if response.status_code != 200:
            raise LivePocError(f"gateway_http_{response.status_code}")
        if len(response.content) > 1_048_576:
            raise LivePocError("provider_response_too_large")
        if response.headers.get("x-poc-tenant-id") != scope.tenant_id:
            raise LivePocError("gateway_tenant_mismatch")
        try:
            completion = _Completion.model_validate(response.json())
        except (ValueError, ValidationError):
            raise LivePocError("invalid_provider_response") from None
        usage = completion.usage
        details = usage.prompt_tokens_details if usage else None
        return ModelReply(
            content=completion.choices[0].message.content,
            requested_model="chat",
            reported_models=(completion.model,) if completion.model else (),
            deployment=self.settings.model_deployment,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            cached_input_tokens=details.cached_tokens if details else None,
        )

    async def forget(self, scope: SessionKey) -> None:
        # History belongs to RunService; failed sessions cannot be used again.
        del scope

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
