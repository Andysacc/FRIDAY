from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    friday_name: str = "FRIDAY"
    default_model: str = "qwen2.5:0.5b"
    ollama_base_url: str = "http://ollama:11434"
    ollama_num_predict: int = Field(
        default=128,
        validation_alias=AliasChoices("OLLAMA_NUM_PREDICT", "OLLAMA_VOICE_NUM_PREDICT"),
    )
    ollama_num_ctx: int = Field(
        default=2048,
        validation_alias=AliasChoices("OLLAMA_NUM_CTX", "OLLAMA_VOICE_NUM_CTX"),
    )
    ollama_num_thread: int | None = Field(
        default=None,
        validation_alias=AliasChoices("OLLAMA_NUM_THREAD", "OLLAMA_VOICE_NUM_THREAD"),
    )
    ollama_keep_alive: str = Field(
        default="5m",
        validation_alias=AliasChoices("OLLAMA_KEEP_ALIVE", "OLLAMA_VOICE_KEEP_ALIVE"),
    )
    livekit_url: str = "ws://livekit:7880"
    livekit_public_url: str = "ws://localhost:7880"
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_agent_name: str = "friday"

    reminder_poll_seconds: float = Field(default=2.0, ge=0.25, le=60)
    reminder_batch_size: int = Field(default=20, ge=1, le=200)
    reminder_max_attempts: int = Field(default=3, ge=1, le=10)
    reminder_retry_seconds: float = Field(default=30.0, ge=1, le=3600)
    reminder_claim_timeout_seconds: float = Field(default=120.0, ge=10, le=3600)

    data_dir: Path = Path("data")
    execution_workspace: Path = Path(".")

    # Multi-tool planning needs more JSON output and schema context than chat.
    harness_num_predict: int = Field(default=2048, ge=256, le=8192)
    harness_num_ctx: int = Field(default=8192, ge=4096, le=32768)

    # Local research pipeline. The coordinator stays intentionally small while
    # long-running synthesis is isolated on a larger Ollama model.
    research_model: str = "qwen3:8b"
    research_embedding_model: str = "bge-m3"
    research_num_predict: int = Field(default=4096, ge=256, le=32768)
    # 8K keeps Qwen3 8B within an 8 GB Docker allocation. Scholar uses a
    # hierarchical map/reduce pipeline instead of requiring one giant context.
    research_num_ctx: int = Field(default=8192, ge=4096, le=262144)
    research_timeout_seconds: float = Field(default=1800, ge=120, le=7200)
    research_keep_alive: str = "30m"
    research_poll_seconds: float = Field(default=2.0, ge=0.25, le=60)
    research_analysis_limit: int = Field(default=120, ge=10, le=1000)
    research_dir: Path | None = None
    searxng_url: str = "http://searxng:8080"
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "friday_research"
    openalex_api_key: str | None = None
    semantic_scholar_api_key: str | None = None
    crossref_mailto: str | None = None

    @property
    def resolved_research_dir(self) -> Path:
        return self.research_dir or (self.data_dir / "Research")


@lru_cache
def get_settings() -> Settings:
    return Settings()
