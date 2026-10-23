from datetime import datetime, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator


Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    role: Role
    content: str


class ChatAttachment(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=8_100)
    truncated: bool = False

    @field_validator("filename")
    @classmethod
    def clean_filename(cls, value: str) -> str:
        value = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not value or value in {".", ".."}:
            raise ValueError("filename is required")
        return value


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float = Field(default=0.7, ge=0, le=2)
    system_prompt: str | None = None
    model: str | None = None
    attachments: list[ChatAttachment] = Field(default_factory=list, max_length=3)
    research_mentions: list[Annotated[str, Field(min_length=1, max_length=180)]] = Field(
        default_factory=list,
        max_length=5,
    )

    @model_validator(mode="after")
    def validate_attachments(self):
        if not self.attachments:
            return self
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("attachments require a user message")
        if sum(len(attachment.content) for attachment in self.attachments) > 24_300:
            raise ValueError("combined attachment text exceeds the 24,300 character limit")
        return self


class FileReadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=14_000_000)
    media_type: str | None = Field(default=None, max_length=160)


class FileReadResponse(BaseModel):
    filename: str
    media_type: str
    size: int
    content: str
    truncated: bool


class ChatResponse(BaseModel):
    model: str
    message: ChatMessage


class ModelListResponse(BaseModel):
    default_model: str
    models: list[str]


ResearchTier = Literal["summary", "fun_fact", "scholar"]
ResearchStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class ResearchEstimateRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=500)
    tier: ResearchTier

    @field_validator("topic")
    @classmethod
    def clean_topic(cls, value: str) -> str:
        value = " ".join(value.split()).strip()
        if len(value) < 2:
            raise ValueError("topic is required")
        return value


class ResearchEstimateResponse(BaseModel):
    topic: str
    tier: ResearchTier
    estimated_seconds: int
    estimated_label: str
    source_target: int
    deep_analysis_target: int
    explanation: str


class ResearchCreateRequest(ResearchEstimateRequest):
    confirmed: bool = False


class ResearchJobRecord(BaseModel):
    job_id: str
    topic: str
    tier: ResearchTier
    status: ResearchStatus
    phase: str
    progress: int
    estimated_seconds: int
    source_target: int
    deep_analysis_target: int
    sources_found: int
    sources_analyzed: int
    artifact_name: str | None
    artifact_media_type: str | None
    knowledge_key: str | None
    error: str | None
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class ResearchListResponse(BaseModel):
    jobs: list[ResearchJobRecord]


class ResearchKnowledgeRecord(BaseModel):
    job_id: str
    topic: str
    knowledge_key: str
    artifact_name: str
    updated_at: datetime


class ProjectUpsertRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=240)
    progress: int = Field(default=0, ge=0, le=100)
    milestone: str = Field(default="Unscheduled", min_length=1, max_length=80)
    status: Literal["on-track", "at-risk", "blocked"] = "on-track"
    pipeline_stage: int = Field(default=0, ge=0, le=4)
    success_criteria: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list,
        max_length=5,
    )


class ProjectRecord(ProjectUpsertRequest):
    project_id: str
    created_at: str
    updated_at: str


class ProjectListResponse(BaseModel):
    projects: list[ProjectRecord]


class ActiveProjectResponse(BaseModel):
    project: ProjectRecord | None


class ProjectTestRequest(BaseModel):
    source: Literal["dashboard", "chat", "voice", "api"] = "api"
    framework: Literal["unittest", "pytest"] = "unittest"
    targets: list[str] = Field(default_factory=lambda: ["tests"], min_length=1, max_length=20)
    model: str | None = None


TodoStatus = Literal["open", "completed"]
TodoPriority = Literal["low", "normal", "high", "critical"]
ReminderChannel = Literal["dashboard", "push", "voice"]
ReminderStatus = Literal["pending", "sent", "failed", "cancelled"]
ReminderDeliveryStatus = Literal["processing", "sent", "failed"]


class TimezoneAwareModel(BaseModel):
    @field_validator("due_at", "scheduled_at", check_fields=False)
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must include a timezone offset")
        return value.astimezone(timezone.utc)


class TodoCreate(TimezoneAwareModel):
    title: str = Field(min_length=1, max_length=160)
    notes: str = Field(default="", max_length=4000)
    project_id: str | None = Field(default=None, min_length=1, max_length=80)
    priority: TodoPriority = "normal"
    due_at: datetime | None = None
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    recurrence_rule: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class TodoUpdate(TimezoneAwareModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    notes: str | None = Field(default=None, max_length=4000)
    project_id: str | None = Field(default=None, min_length=1, max_length=80)
    status: TodoStatus | None = None
    priority: TodoPriority | None = None
    due_at: datetime | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    recurrence_rule: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("title")
    @classmethod
    def clean_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_optional_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def reject_null_for_required_fields(self):
        for field in ("title", "notes", "status", "priority", "timezone"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class TodoRecord(BaseModel):
    todo_id: str
    title: str
    notes: str
    project_id: str | None
    status: TodoStatus
    priority: TodoPriority
    due_at: datetime | None
    timezone: str
    recurrence_rule: str | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int


class TodoListResponse(BaseModel):
    todos: list[TodoRecord]


class ReminderCreate(TimezoneAwareModel):
    scheduled_at: datetime
    channel: ReminderChannel = "dashboard"


class ReminderUpdate(TimezoneAwareModel):
    scheduled_at: datetime | None = None
    channel: ReminderChannel | None = None
    status: Literal["pending", "cancelled"] | None = None

    @model_validator(mode="after")
    def reject_null_fields(self):
        for field in ("scheduled_at", "channel", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ReminderRecord(BaseModel):
    reminder_id: str
    todo_id: str
    scheduled_at: datetime
    channel: ReminderChannel
    status: ReminderStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class ReminderListResponse(BaseModel):
    reminders: list[ReminderRecord]


class ReminderDeliveryRecord(BaseModel):
    delivery_id: str
    reminder_id: str
    todo_id: str
    channel: ReminderChannel
    scheduled_at: datetime
    status: ReminderDeliveryStatus
    attempt: int
    payload: dict[str, object]
    error: str | None
    delivered_at: datetime | None
    acknowledged_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReminderDeliveryListResponse(BaseModel):
    deliveries: list[ReminderDeliveryRecord]
