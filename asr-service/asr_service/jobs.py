from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import config
from .db import JobStore, utc_now
from .engine import AudioDecodeError, FunASREngine

logger = logging.getLogger(__name__)


class JobManager:
    def __init__(self, store: JobStore, engine: FunASREngine | None) -> None:
        self.store = store
        self.engine = engine
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=config.QUEUE_MAX_SIZE)
        self.worker_task: asyncio.Task[None] | None = None
        self.current_task_id: str | None = None

    async def start(self) -> None:
        config.JOBS_ROOT.mkdir(parents=True, exist_ok=True)
        self.store.recover_incomplete()
        self.worker_task = asyncio.create_task(self._worker(), name="asr-gpu-worker")

    async def stop(self) -> None:
        if self.worker_task is not None:
            self.worker_task.cancel()
            await asyncio.gather(self.worker_task, return_exceptions=True)
            self.worker_task = None

    async def enqueue(self, task_id: str) -> None:
        await self.queue.put(task_id)

    def queue_length(self) -> int:
        return self.queue.qsize()

    async def cancel(self, task_id: str) -> dict[str, Any] | None:
        record = self.store.get(task_id)
        if record is None:
            return None
        if record["status"] == "queued":
            self.store.update(
                task_id,
                status="cancelled",
                finished_at=utc_now(),
                progress=0,
            )
            return self.store.get(task_id)
        return record

    async def _worker(self) -> None:
        while True:
            task_id = await self.queue.get()
            try:
                record = self.store.get(task_id)
                if not record or record["status"] != "queued":
                    continue
                self.current_task_id = task_id
                await asyncio.to_thread(self._run_job, task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled ASR worker error for %s", task_id)
            finally:
                self.current_task_id = None
                self.queue.task_done()

    def _run_job(self, task_id: str) -> None:
        record = self.store.get(task_id)
        if record is None:
            return
        self.store.update(task_id, status="processing", progress=1, started_at=utc_now())
        task_dir = config.JOBS_ROOT / task_id
        input_path = Path(record["input_path"])
        normalized_path = task_dir / "normalized.wav"
        try:
            if self.engine is None:
                raise RuntimeError("FunASR 模型未加载，服务尚未就绪。")
            self.store.update(task_id, progress=5)
            decode_started = time.perf_counter()
            FunASREngine.normalize_audio(input_path, normalized_path)
            decode_seconds = time.perf_counter() - decode_started
            self.store.update(task_id, progress=10)

            def progress(current: int, total: int) -> None:
                if total > 0:
                    value = min(95, 10 + int(current / total * 85))
                    self.store.update(task_id, progress=value)

            result = self.engine.transcribe(
                normalized_path,
                record.get("hotwords") or [],
                progress_callback=progress,
            )
            metrics = result.get("metrics") or {}
            metrics["decode_seconds"] = decode_seconds
            metrics["total_seconds"] = decode_seconds + (metrics.get("inference_seconds") or 0)
            result["metrics"] = metrics
            (task_dir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.store.update(
                task_id,
                status="succeeded",
                progress=100,
                finished_at=utc_now(),
                result_json=json.dumps(result, ensure_ascii=False),
                metrics_json=json.dumps(metrics, ensure_ascii=False),
            )
        except AudioDecodeError as exc:
            self._fail(task_id, "INVALID_AUDIO", str(exc))
        except ValueError as exc:
            self._fail(task_id, "INVALID_REQUEST", str(exc))
        except Exception as exc:
            logger.exception("ASR inference failed for %s", task_id)
            self._fail(task_id, "INFERENCE_FAILED", str(exc))

    def _fail(self, task_id: str, code: str, message: str) -> None:
        self.store.update(
            task_id,
            status="failed",
            finished_at=utc_now(),
            error_code=code,
            error_message=message,
        )

    def delete_files(self, task_id: str) -> None:
        task_dir = config.JOBS_ROOT / task_id
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)
