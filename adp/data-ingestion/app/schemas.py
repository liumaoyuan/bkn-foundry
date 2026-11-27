from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ControlDatabaseBootstrapRequest(BaseModel):
    database_url: str = Field(min_length=1, max_length=4000)
    create_database: bool = True
    bootstrap_database_url: str = Field(default="", max_length=4000)


class ControlDatabaseStatusOut(BaseModel):
    configured: bool
    initialized: bool
    source: str
    database_label: str = ""
    error: str = ""
    bootstrap_enabled: bool = True


class ControlDatabaseTestOut(BaseModel):
    connected: bool
    database_exists: bool
    database_name: str
    database_label: str


class DataSpaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    database_url: str = Field(min_length=1, max_length=4000)


class DataSpaceOut(ORMModel):
    id: str
    name: str
    slug: str
    database_label: str
    is_default: bool
    status: str
    last_error: str
    created_at: datetime


class DataSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_type: str = "upload"
    config: dict[str, Any] = Field(default_factory=dict)


class DataSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    source_type: str | None = None
    config: dict[str, Any] | None = None


class DatabaseCatalogRequest(BaseModel):
    source_id: str | None = None
    source_type: str = "mysql"
    config: dict[str, Any] = Field(default_factory=dict)
    table_name: str = ""


class DataSourceOut(ORMModel):
    id: str
    name: str
    source_type: str
    config: dict[str, Any]
    status: str
    last_checked_at: datetime | None
    created_at: datetime


class McpToolCreate(BaseModel):
    name: str = Field(
        min_length=2,
        max_length=80,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$",
    )
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    read_only: bool = True
    requires_approval: bool = True
    tool_type: str = Field(default="http", pattern=r"^http$")
    config: dict[str, Any]
    enabled: bool = True


class McpToolUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    read_only: bool | None = None
    requires_approval: bool | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class McpToolExecuteRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class SkillCreate(BaseModel):
    name: str = Field(
        min_length=2,
        max_length=80,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$",
    )
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    content: str = Field(min_length=1, max_length=100000)
    enabled: bool = True


class SkillUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    content: str | None = Field(default=None, min_length=1, max_length=100000)
    enabled: bool | None = None


class DatasetOut(ORMModel):
    id: str
    name: str
    slug: str
    description: str
    data_type: str
    target_table: str
    storage_mode: str
    upsert_keys: list[str]
    physical_table: str
    partition_mode: str
    record_count: int
    last_ingested_at: datetime | None
    created_at: datetime


class DatasetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    data_type: str | None = None
    upsert_keys: list[str] | None = None


class TemplateOut(ORMModel):
    id: str
    code: str
    name: str
    description: str
    supported_types: list[str]
    steps: list[dict[str, Any]]
    enabled: bool


class PipelineTemplateCreate(BaseModel):
    code: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^user-[a-zA-Z0-9][a-zA-Z0-9_-]*$",
    )
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    supported_types: list[str] = Field(default_factory=lambda: ["csv", "excel"])
    steps: list[dict[str, Any]] = Field(min_length=1, max_length=50)
    enabled: bool = True


class PipelineTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    supported_types: list[str] | None = None
    steps: list[dict[str, Any]] | None = Field(default=None, min_length=1, max_length=50)
    enabled: bool | None = None


class PipelineRunRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    step_inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PipelineExecutionCreate(BaseModel):
    pipeline_code: str = Field(min_length=1, max_length=64)
    pipeline_name: str = Field(min_length=1, max_length=120)
    inputs: dict[str, Any] = Field(default_factory=dict)
    step_inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PipelineExecutionUpdate(BaseModel):
    status: str | None = None
    results: list[dict[str, Any]] | None = None
    error_message: str | None = None


class StageOut(ORMModel):
    id: str
    sequence: int
    stage_key: str
    name: str
    status: str
    input_count: int
    output_count: int
    duration_ms: int
    summary: dict[str, Any]
    logs: list[str]
    started_at: datetime | None
    finished_at: datetime | None


class RunOut(ORMModel):
    id: str
    dataset_id: str
    source_id: str | None
    template_code: str
    status: str
    original_name: str
    file_type: str
    media_type: str
    source_object: str
    source_context: dict[str, Any] | None
    file_size: int
    fingerprint: str
    fetched_count: int
    inserted_count: int
    updated_count: int
    duplicate_count: int
    error_count: int
    incremental_cursor_before: str
    incremental_cursor_after: str
    error_message: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    stages: list[StageOut]


class RunListItem(ORMModel):
    id: str
    dataset_id: str
    source_id: str | None
    template_code: str
    status: str
    original_name: str
    file_type: str
    source_object: str
    file_size: int
    fetched_count: int
    inserted_count: int
    updated_count: int
    duplicate_count: int
    error_count: int
    incremental_cursor_before: str
    incremental_cursor_after: str
    error_message: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class DatasetRecordOut(ORMModel):
    id: str
    dataset_id: str
    run_id: str
    record_key: str
    data_type: str
    payload: dict[str, Any]
    asset_path: str
    source_id: str | None
    source_object: str
    source_locator: dict[str, Any] | None
    stable_record_id: str
    created_at: datetime


class DatasetFieldSchema(BaseModel):
    name: str
    data_type: str
    nullable: bool
    present_count: int
    completeness: float
    sample_values: list[Any]
    primary_key: bool


class DatasetSchemaOut(BaseModel):
    schema_version: str
    sampled_records: int
    total_records: int
    primary_key_fields: list[str]
    stable_key_strategy: str
    fields: list[DatasetFieldSchema]


class DatasetRecordChangesOut(BaseModel):
    items: list[DatasetRecordOut]
    next_cursor: str | None
    has_more: bool


class SheetPreview(BaseModel):
    name: str
    index: int
    state: str
    is_active: bool
    columns: list[str]
    rows: list[dict[str, Any]]
    metadata: dict[str, Any]
    warnings: list[str]


class InspectionOut(BaseModel):
    filename: str
    file_type: str
    media_type: str
    size: int
    recommended_pipeline: str
    preview_type: str
    columns: list[str]
    rows: list[dict[str, Any]]
    metadata: dict[str, Any]
    warnings: list[str]
    sheets: list[SheetPreview] = Field(default_factory=list)


class AgentSessionCreate(BaseModel):
    title: str = "新会话"


class AgentMessageOut(ORMModel):
    id: str
    role: str
    content: str
    attachments: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    tool_call_id: str
    name: str
    created_at: datetime


class AgentApprovalOut(ORMModel):
    id: str
    session_id: str
    message_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: str
    reason: str
    result: dict[str, Any]
    created_at: datetime
    resolved_at: datetime | None


class AgentMemoryOut(ORMModel):
    id: str
    memory_key: str
    value: str
    created_at: datetime
    updated_at: datetime


class AgentSessionOut(ORMModel):
    id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime


class AgentSessionDetail(AgentSessionOut):
    messages: list[AgentMessageOut]
    approvals: list[AgentApprovalOut]
    memories: list[AgentMemoryOut]


class AgentTurnRequest(BaseModel):
    content: str = Field(min_length=1, max_length=50000)


class AgentApprovalRequest(BaseModel):
    reason: str = ""


class AgentMemoryCreate(BaseModel):
    key: str = Field(min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=10000)


class AutoScanJobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    source_id: str
    dataset_id: str
    template_code: str = "auto"
    scan_type: str
    path: str = ""
    recursive: bool = False
    file_pattern: str = "*"
    interval_seconds: int = Field(default=300, ge=30)
    schedule_type: str = "interval"
    schedule_time: str = "02:00"
    weekdays: list[int] = Field(default_factory=list)
    cron_expression: str = ""
    timezone: str = "Asia/Shanghai"
    enabled: bool = True


class AutoScanJobUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    dataset_id: str | None = None
    template_code: str | None = None
    path: str | None = None
    recursive: bool | None = None
    file_pattern: str | None = None
    interval_seconds: int | None = Field(default=None, ge=30)
    schedule_type: str | None = None
    schedule_time: str | None = None
    weekdays: list[int] | None = None
    cron_expression: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
