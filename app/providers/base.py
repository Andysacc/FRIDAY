from abc import ABC, abstractmethod

from app.schemas import ChatMessage


class ProviderError(RuntimeError):
    pass


class AIProvider(ABC):
    @abstractmethod
    async def chat(
        self,
        *,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
    ) -> str:
        raise NotImplementedError
