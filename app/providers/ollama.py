import json
from collections.abc import AsyncIterator

import httpx

from app.providers.base import AIProvider, ProviderError
from app.schemas import ChatMessage


class OllamaProvider(AIProvider):
    def __init__(
        self,
        base_url: str,
        *,
        num_predict: int = 128,
        num_ctx: int = 2048,
        num_thread: int | None = None,
        keep_alive: str = "5m",
        timeout_seconds: float = 120,
        think: bool | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.num_predict = num_predict
        self.num_ctx = num_ctx
        self.num_thread = num_thread
        self.keep_alive = keep_alive
        self.timeout_seconds = timeout_seconds
        self.think = think

    def _options(self, temperature: float) -> dict[str, float | int]:
        options: dict[str, float | int] = {
            "temperature": temperature,
            "num_predict": self.num_predict,
            "num_ctx": self.num_ctx,
        }
        if self.num_thread is not None:
            options["num_thread"] = self.num_thread
        return options

    async def chat(
        self,
        *,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
    ) -> str:
        payload = {
            "model": model,
            "messages": [message.model_dump() for message in messages],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": self._options(temperature),
        }
        if self.think is not None:
            payload["think"] = self.think
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip()[:1000]
            raise ProviderError(
                f"Ollama request failed ({exc.response.status_code}): {detail or exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama request failed: {exc}") from exc

        data = response.json()
        return data.get("message", {}).get("content", "")

    async def list_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not list Ollama models: {exc}") from exc

        models = response.json().get("models", [])
        return [model["name"] for model in models if isinstance(model.get("name"), str)]

    async def stream_chat(
        self,
        *,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
    ) -> AsyncIterator[str]:
        payload = {
            "model": model,
            "messages": [message.model_dump() for message in messages],
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": self._options(temperature),
        }
        if self.think is not None:
            payload["think"] = self.think
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, read=None)) as client:
                async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue

                        data = json.loads(line)
                        if data.get("done"):
                            break

                        content = data.get("message", {}).get("content", "")
                        if content:
                            yield content
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Ollama stream failed: {exc}") from exc
