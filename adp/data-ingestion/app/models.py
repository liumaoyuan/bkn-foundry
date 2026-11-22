from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid4().hex[:16]


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC without offsets and restore UTC for every database read."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, _dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class DataSpace(Base):
    __tablename__ = "data_spaces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    database_url_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    database_label: Mapped[str] = mapped_column(String(255), default="")
    is_default: Mapped[bool] = mapped_column(default=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="ready", index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="ready")
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    data_type: Mapped[str] = mapped_column(String(32), default="mixed")
    target_table: Mapped[str] = mapped_column(String(120), default="")
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    last_ingested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )

    records: Mapped[list[DatasetRecord]] = relationship(
        back_populates="dataset",
        cascade="all, delete-orphan",
    )


class PipelineTemplate(Base):
    __tablename__ = "pipeline_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    supported_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )


class AutoScanJob(Base):
    __tablename__ = "auto_scan_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("data_sources.id"),
        nullable=False,
        index=True,
    )
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id"),
        nullable=False,
        index=True,
    )
    template_code: Mapped[str] = mapped_column(
        String(64),
        default="auto",
    )
    scan_type: Mapped[str] = mapped_column(String(32), nullable=False)
    path: Mapped[str] = mapped_column(Text, default="")
    recursive: Mapped[bool] = mapped_column(default=False)
    file_pattern: Mapped[str] = mapped_column(String(160), default="*")
    interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    schedule_type: Mapped[str] = mapped_column(String(24), default="interval")
    schedule_time: Mapped[str] = mapped_column(String(5), default="02:00")
    weekdays: Mapped[list[int]] = mapped_column(JSON, default=list)
    cron_expression: Mapped[str] = mapped_column(String(160), default="")
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai")
    enabled: Mapped[bool] = mapped_column(default=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="ready")
    last_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_scan_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        index=True,
    )
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_run_id: Mapped[str | None] = mapped_column(String(32))
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    scan_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
    )

    source: Mapped[DataSource] = relationship()
    dataset: Mapped[Dataset] = relationship()


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id"),
        nullable=False,
        index=True,
    )
    source_id: Mapped[str | None] = mapped_column(
        ForeignKey("data_sources.id"),
        index=True,
    )
    template_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(32), default="unknown")
    media_type: Mapped[str] = mapped_column(String(120), default="")
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    fingerprint: Mapped[str] = mapped_column(String(64), default="", index=True)
    source_path: Mapped[str] = mapped_column(Text, default="")
    artifact_path: Mapped[str] = mapped_column(Text, default="")
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        index=True,
    )

    dataset: Mapped[Dataset] = relationship()
    source: Mapped[DataSource | None] = relationship()
    stages: Mapped[list[StageRun]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="StageRun.sequence",
    )
    assets: Mapped[list[DataAsset]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class StageRun(Base):
    __tablename__ = "stage_runs"
    __table_args__ = (
        UniqueConstraint("run_id", "stage_key", name="uq_run_stage_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_runs.id"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    input_count: Mapped[int] = mapped_column(Integer, default=0)
    output_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    logs: Mapped[list[str]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    run: Mapped[IngestionRun] = relationship(back_populates="stages")


class DatasetRecord(Base):
    __tablename__ = "dataset_records"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "fingerprint",
            name="uq_dataset_record_fingerprint",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_runs.id"),
        nullable=False,
        index=True,
    )
    fingerprint: Mapped[str] = mapped_column(String(96), nullable=False)
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    asset_path: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )

    dataset: Mapped[Dataset] = relationship(back_populates="records")


class DataAsset(Base):
    __tablename__ = "data_assets"
    __table_args__ = (
        UniqueConstraint("run_id", "sha256", name="uq_run_asset_sha256"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_runs.id"),
        nullable=False,
        index=True,
    )
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), default="")
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="source")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )

    run: Mapped[IngestionRun] = relationship(back_populates="assets")


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    title: Mapped[str] = mapped_column(String(200), default="新会话")
    status: Mapped[str] = mapped_column(String(32), default="idle", index=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        index=True,
    )

    messages: Mapped[list[AgentMessage]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AgentMessage.created_at",
    )
    approvals: Mapped[list[AgentApproval]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AgentApproval.created_at",
    )
    memories: Mapped[list[AgentMemory]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AgentMemory.updated_at",
    )


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    tool_call_id: Mapped[str] = mapped_column(String(160), default="")
    name: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        index=True,
    )

    session: Mapped[AgentSession] = relationship(back_populates="messages")


class AgentApproval(Base):
    __tablename__ = "agent_approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id"),
        nullable=False,
        index=True,
    )
    message_id: Mapped[str] = mapped_column(
        ForeignKey("agent_messages.id"),
        nullable=False,
        index=True,
    )
    tool_call_id: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        index=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    session: Mapped[AgentSession] = relationship(back_populates="approvals")


class AgentMemory(Base):
    __tablename__ = "agent_memories"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "memory_key",
            name="uq_agent_memory_session_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id"),
        nullable=False,
        index=True,
    )
    memory_key: Mapped[str] = mapped_column(String(160), nullable=False)
    value: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
    )

    session: Mapped[AgentSession] = relationship(back_populates="memories")
