from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


JobStatus = Literal["queued", "processing", "succeeded", "failed", "cancelled"]


class Segment(BaseModel):
    start_ms: int
    end_ms: int
    text: str
    speaker: str | int | None = None


class ModelInfo(BaseModel):
    asr: str
    asr_id: str
    vad: str | None = None
    vad_id: str | None = None
    punc: str | None = None
    punc_id: str | None = None
    device: str
    gpu_available: bool
    speaker_enabled: bool = False
    hotwords_enabled: bool = False


class JobMetrics(BaseModel):
    audio_seconds: float | None = None
    model_load_seconds: float | None = None
    decode_seconds: float | None = None
    inference_seconds: float | None = None
    total_seconds: float | None = None
    rtf: float | None = None
    peak_allocated_mib: float | None = None
    peak_reserved_mib: float | None = None


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    api_version: str = "v1"
    task_id: str
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    filename: str | None = None
    text: str | None = None
    segments: list[Segment] = Field(default_factory=list)
    timestamps: list[Any] = Field(default_factory=list)
    model: ModelInfo | None = None
    metrics: JobMetrics | None = None
    error: dict[str, Any] | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class HealthResponse(BaseModel):
    api_version: str = "v1"
    api_ok: bool
    ready: bool
    model_loaded: bool
    model_load_error: str | None = None
    device: str
    gpu_available: bool
    queue_length: int
    current_task_id: str | None
    model_name: str
    service_version: str
