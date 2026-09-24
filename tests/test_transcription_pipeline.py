import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class FakeDownloader:
    def __init__(self):
        self.started = False
        self.temp_dir = None
        self.respect_config = True

    def ensure_started(self, directory, respect_config=False):
        self.started = True
        self.temp_dir = Path(directory)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "audio.m4a").write_bytes(b"fake audio")
        return True, ""

    def request(self, path, method="GET", data=None):
        if path == "/api/v1/tasks" and method == "POST":
            return 200, {"id": "download-id"}
        if path == "/api/v1/tasks/download-id":
            return 200, {"status": "Finished", "progress": 1, "isSuccessful": True}
        raise AssertionError(f"Unexpected downloader request: {method} {path}")

    def stop(self):
        pass


class TranscriptionPipelineProviderTests(unittest.TestCase):
    def setUp(self):
        self.task_id = "a" * 24
        with app.TRANSCRIPTION_LOCK:
            self.previous_task = app.TRANSCRIPTION_TASKS.pop(self.task_id, None)
        self.previous_active = app.MIMO_ACTIVE_TASK_ID
        app.MIMO_ACTIVE_TASK_ID = ""
        with app.MIMO_QUEUE_CONDITION:
            self.previous_queue = list(app.MIMO_QUEUE)
            app.MIMO_QUEUE.clear()

    def tearDown(self):
        with app.TRANSCRIPTION_LOCK:
            app.TRANSCRIPTION_TASKS.pop(self.task_id, None)
        if self.previous_task is not None:
            with app.TRANSCRIPTION_LOCK:
                app.TRANSCRIPTION_TASKS[self.task_id] = self.previous_task
        with app.MIMO_QUEUE_CONDITION:
            app.MIMO_QUEUE[:] = self.previous_queue
        app.MIMO_ACTIVE_TASK_ID = self.previous_active
        app.TRANSCRIPTION_SERVICES.pop(self.task_id, None)

    def add_task(self, provider):
        with app.TRANSCRIPTION_LOCK:
            app.TRANSCRIPTION_TASKS[self.task_id] = {
                "task_id": self.task_id,
                "provider": provider,
                "model": provider,
                "source_title": "测试视频",
                "cancel_requested": False,
                "status": "queued",
            }

    def update_in_memory(self, task_id, **fields):
        task = app.TRANSCRIPTION_TASKS[task_id]
        task.update(fields)
        return dict(task)

    def test_mimo_pipeline_downloads_and_uses_cloud_without_local_health(self):
        self.add_task("mimo-v2.6-flash")
        downloader = FakeDownloader()
        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(app, "transcription_root", return_value=Path(root)),
                patch.object(app, "BBDownService", return_value=downloader),
                patch.object(app, "asr_json", return_value=(503, {"error": "local ASR offline"})) as local_health,
                patch.object(app, "update_transcription", side_effect=self.update_in_memory),
                patch.object(app, "acquire_mimo_queue", wraps=app.acquire_mimo_queue) as enter_mimo_queue,
                patch.object(app, "mimo_transcribe_audio", return_value={"text": "cloud transcript"}) as cloud_transcribe,
            ):
                app.run_transcription_pipeline(self.task_id, {"url": "https://example.test/video", "title": "测试视频"})

        local_health.assert_not_called()
        self.assertTrue(downloader.started)
        enter_mimo_queue.assert_called_once_with(self.task_id)
        cloud_transcribe.assert_called_once()
        self.assertEqual(cloud_transcribe.call_args.args[2:4], ("mimo-v2.6-flash", "mimo-v2.6-flash"))
        self.assertEqual(app.TRANSCRIPTION_TASKS[self.task_id]["status"], "succeeded")

    def test_local_pipeline_still_requires_local_health(self):
        self.add_task("local")
        downloader = FakeDownloader()
        updates = []

        def record_update(task_id, **fields):
            updates.append(fields)
            return self.update_in_memory(task_id, **fields)

        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(app, "transcription_root", return_value=Path(root)),
                patch.object(app, "BBDownService", return_value=downloader),
                patch.object(app, "asr_json", return_value=(503, {"error": "local ASR offline"})) as local_health,
                patch.object(app, "update_transcription", side_effect=record_update),
            ):
                app.run_transcription_pipeline(self.task_id, {"url": "https://example.test/video", "title": "测试视频"})

        local_health.assert_called_once_with("/health")
        self.assertFalse(downloader.started)
        self.assertTrue(any(item.get("status") == "failed" for item in updates))


if __name__ == "__main__":
    unittest.main()
