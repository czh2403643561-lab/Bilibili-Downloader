from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import config
from .db import JobStore
from .engine import AudioDecodeError, FunASREngine
from .jobs import JobManager
from .schemas import HealthResponse, JobResponse, Segment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def parse_hotwords(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError:
        parsed = [item.strip() for item in value.replace("，", ",").split(",")]
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise HTTPException(status_code=422, detail="hotwords 必须是 JSON 字符串数组。")
    cleaned = []
    for item in parsed:
        item = item.strip()
        if item and item not in cleaned:
            cleaned.append(item)
    if len(cleaned) > 500:
        raise HTTPException(status_code=422, detail="单个任务最多 500 个热词。")
    return cleaned


def to_response(record: dict[str, Any]) -> JobResponse:
    result = record.get("result") or {}
    model = result.get("model")
    metrics = result.get("metrics")
    segments = [Segment.model_validate(item) for item in result.get("segments") or []]
    return JobResponse(
        task_id=record["task_id"],
        status=record["status"],
        progress=int(record.get("progress") or 0),
        filename=record.get("filename"),
        text=result.get("text"),
        segments=segments,
        timestamps=result.get("timestamps") or [],
        model=model,
        metrics=metrics,
        error=record.get("error"),
        created_at=record.get("created_at"),
        started_at=record.get("started_at"),
        finished_at=record.get("finished_at"),
    )


async def save_upload(upload: UploadFile, path: Path) -> int:
    total = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("wb") as target:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="上传文件过大。")
                target.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    return total


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.DATA_ROOT.mkdir(parents=True, exist_ok=True)
    config.JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    store = JobStore(config.DB_PATH)
    engine = None
    model_error = None
    try:
        engine = await asyncio.to_thread(FunASREngine)
    except Exception as exc:
        model_error = str(exc)
        logger.exception("FunASR model failed to load during startup")
    manager = JobManager(store, engine)
    await manager.start()
    app.state.store = store
    app.state.engine = engine
    app.state.model_error = model_error
    app.state.manager = manager
    try:
        yield
    finally:
        await manager.stop()
        store.close()


app = FastAPI(
    title="Local FunASR Service",
    version=config.SERVICE_VERSION,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"]
)


@app.get("/health", response_model=HealthResponse)
async def health() -> JSONResponse:
    manager: JobManager = app.state.manager
    engine: FunASREngine | None = app.state.engine
    ready = engine is not None
    payload = HealthResponse(
        api_ok=True,
        ready=ready,
        model_loaded=ready,
        model_load_error=app.state.model_error,
        device=config.DEVICE,
        gpu_available=bool(torch.cuda.is_available()),
        queue_length=manager.queue_length(),
        current_task_id=manager.current_task_id,
        model_name=config.ASR_MODEL_ID,
        service_version=config.SERVICE_VERSION,
    )
    return JSONResponse(
        status_code=200 if ready else 503,
        content=payload.model_dump(mode="json"),
    )


@app.post("/v1/jobs", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    file: UploadFile = File(...),
    hotwords: str | None = Form(default=None),
) -> JobResponse:
    manager: JobManager = app.state.manager
    if app.state.engine is None:
        raise HTTPException(status_code=503, detail="FunASR 模型尚未加载。")
    original_name = Path(file.filename or "audio").name
    extension = Path(original_name).suffix.lower()
    if extension not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"不支持的音频格式：{extension or '无扩展名'}")
    if manager.queue.full():
        raise HTTPException(status_code=503, detail="GPU 任务队列已满，请稍后重试。")
    parsed_hotwords = parse_hotwords(hotwords)
    task_id = uuid.uuid4().hex
    task_dir = config.JOBS_ROOT / task_id
    input_path = task_dir / f"input{extension}"
    await save_upload(file, input_path)
    try:
        await asyncio.to_thread(FunASREngine.probe_duration, input_path)
    except AudioDecodeError as exc:
        shutil.rmtree(task_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record = app.state.store.create(
        task_id=task_id,
        filename=original_name,
        input_path=str(input_path),
        hotwords=parsed_hotwords,
    )
    await manager.enqueue(task_id)
    return to_response(record)


@app.get("/v1/jobs/{task_id}", response_model=JobResponse)
async def get_job(task_id: str) -> JobResponse:
    record = app.state.store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return to_response(record)


@app.get("/v1/jobs/{task_id}/result", response_model=JobResponse)
async def get_result(task_id: str) -> JobResponse:
    record = app.state.store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    if record["status"] != "succeeded":
        raise HTTPException(
            status_code=409,
            detail={"status": record["status"], "error": record.get("error")},
        )
    return to_response(record)


@app.post("/v1/jobs/{task_id}/cancel", response_model=JobResponse)
async def cancel_job(task_id: str) -> JobResponse:
    record = await app.state.manager.cancel(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    if record["status"] == "processing":
        raise HTTPException(status_code=409, detail="任务已开始 GPU 推理，当前不能中断。")
    return to_response(record)


@app.delete("/v1/jobs/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(task_id: str) -> None:
    record = app.state.store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    if record["status"] in {"queued", "processing"}:
        raise HTTPException(status_code=409, detail="任务仍在队列或处理中，不能删除。")
    app.state.manager.delete_files(task_id)
    app.state.store.update(task_id, status="cancelled", finished_at=record.get("finished_at") or None)
