from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30.0
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    filename TEXT,
                    input_path TEXT NOT NULL,
                    hotwords_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    result_json TEXT,
                    metrics_json TEXT
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)"
            )
            self._connection.commit()

    def create(
        self,
        task_id: str,
        filename: str,
        input_path: str,
        hotwords: list[str],
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO jobs
                (task_id, status, progress, filename, input_path, hotwords_json, created_at)
                VALUES (?, 'queued', 0, ?, ?, ?, ?)
                """,
                (task_id, filename, input_path, json.dumps(hotwords, ensure_ascii=False), now),
            )
            self._connection.commit()
        return self.get(task_id)  # type: ignore[return-value]

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["hotwords"] = json.loads(item.pop("hotwords_json") or "[]")
        item["result"] = json.loads(item.pop("result_json") or "null")
        item["metrics"] = json.loads(item.pop("metrics_json") or "null")
        if item.get("error_code") or item.get("error_message"):
            item["error"] = {
                "code": item.pop("error_code"),
                "message": item.pop("error_message"),
            }
        else:
            item.pop("error_code", None)
            item.pop("error_message", None)
            item["error"] = None
        return item

    def update(self, task_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "progress",
            "started_at",
            "finished_at",
            "error_code",
            "error_message",
            "result_json",
            "metrics_json",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._lock:
            self._connection.execute(
                f"UPDATE jobs SET {assignments} WHERE task_id = ?",
                (*updates.values(), task_id),
            )
            self._connection.commit()

    def recover_incomplete(self) -> int:
        now = utc_now()
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET status='failed', finished_at=?, error_code='SERVICE_RESTARTED',
                    error_message='服务重启时未完成，任务未自动恢复。'
                WHERE status IN ('queued', 'processing')
                """,
                (now,),
            )
            self._connection.commit()
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._connection.close()
