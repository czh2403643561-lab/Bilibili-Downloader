import base64
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app


def completion(finish_reason="stop", text="transcript"):
    return 200, {"choices": [{"finish_reason": finish_reason, "message": {"content": text}}]}


class MiMoContentFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.task_ids = ["filter-task-1", "filter-task-2"]
        with app.TRANSCRIPTION_LOCK:
            self.previous_tasks = {task_id: app.TRANSCRIPTION_TASKS.pop(task_id, None) for task_id in self.task_ids}
        with app.MIMO_QUEUE_CONDITION:
            self.previous_queue = list(app.MIMO_QUEUE)
            self.previous_active = app.MIMO_ACTIVE_TASK_ID
            app.MIMO_QUEUE.clear()
            app.MIMO_ACTIVE_TASK_ID = ""
        for task_id in self.task_ids:
            with app.TRANSCRIPTION_LOCK:
                app.TRANSCRIPTION_TASKS[task_id] = {
                    "task_id": task_id,
                    "provider": "mimo-v2.6-flash",
                    "model": "mimo-v2.6-flash",
                    "cancel_requested": False,
                    "status": "queued",
                }

    def tearDown(self):
        with app.TRANSCRIPTION_LOCK:
            for task_id in self.task_ids:
                app.TRANSCRIPTION_TASKS.pop(task_id, None)
                if self.previous_tasks[task_id] is not None:
                    app.TRANSCRIPTION_TASKS[task_id] = self.previous_tasks[task_id]
        with app.MIMO_QUEUE_CONDITION:
            app.MIMO_QUEUE[:] = self.previous_queue
            app.MIMO_ACTIVE_TASK_ID = self.previous_active
        self.temp.cleanup()

    def make_chunk(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return path

    def request_chunk_id(self, args):
        encoded = args[2]["messages"][0]["content"][0]["input_audio"]["data"].split(",", 1)[1]
        return base64.b64decode(encoded).decode("utf-8")

    def test_normal_stop_completes_chunks(self):
        chunks = [self.make_chunk("one.mp3", "one"), self.make_chunk("two.mp3", "two")]
        with (
            patch.object(app, "load_mimo_api_key", return_value="test-key"),
            patch.object(app, "mimo_audio_chunks", return_value=chunks),
            patch.object(app, "mimo_request", return_value=completion("stop", "ok")) as request,
        ):
            result = app.mimo_transcribe_audio(self.root / "audio.m4a", self.root / "work", "mimo-v2.6-flash", "mimo-v2.6-flash")
        self.assertEqual(result["text"], "ok\nok")
        self.assertEqual(request.call_count, 2)

    def test_content_filter_retries_only_failed_chunk_at_smaller_sizes(self):
        first, filtered = self.make_chunk("one.mp3", "already-done"), self.make_chunk("two.mp3", "filtered")
        child_a = self.make_chunk("300/child-a.mp3", "child-a")
        child_b = self.make_chunk("300/child-b.mp3", "child-b")
        grand_a = self.make_chunk("150/grand-a.mp3", "grand-a")
        grand_b = self.make_chunk("150/grand-b.mp3", "grand-b")
        calls = {}
        split_sizes = []

        def request(*args, **kwargs):
            identity = self.request_chunk_id(args)
            calls[identity] = calls.get(identity, 0) + 1
            if identity in {"filtered", "child-b"}:
                return completion("content_filter", "")
            return completion("stop", identity)

        def split(chunk, temp_dir, seconds):
            split_sizes.append(seconds)
            return [child_a, child_b] if seconds == 300 else [grand_a, grand_b]

        stages = []
        with (
            patch.object(app, "load_mimo_api_key", return_value="test-key"),
            patch.object(app, "mimo_audio_chunks", return_value=[first, filtered]),
            patch.object(app, "mimo_request", side_effect=request),
            patch.object(app, "split_mimo_chunk", side_effect=split),
        ):
            result = app.mimo_transcribe_audio(
                self.root / "audio.m4a", self.root / "work", "mimo-v2.6-flash", "mimo-v2.6-flash",
                progress_callback=lambda _progress, stage: stages.append(stage),
            )
        self.assertEqual(calls["already-done"], 1)
        self.assertEqual(calls["filtered"], 1)
        self.assertEqual(split_sizes, [300, 150])
        self.assertIn("第 2/2 段触发内容过滤，正在缩小片段重试", "\n".join(stages))
        self.assertIn("already-done", result["text"])
        self.assertIn("grand-a", result["text"])

    def test_exhausted_filter_fails_current_video_and_releases_queue_for_next(self):
        original = self.make_chunk("initial.mp3", "first-task")
        retry300 = [self.make_chunk("retry300/a.mp3", "300-a"), self.make_chunk("retry300/b.mp3", "300-b")]
        retry150 = [self.make_chunk("retry150/a.mp3", "150-a"), self.make_chunk("retry150/b.mp3", "150-b")]
        def update(task_id, **fields):
            app.TRANSCRIPTION_TASKS[task_id].update(fields)
            return dict(app.TRANSCRIPTION_TASKS[task_id])

        def request(*args, **kwargs):
            if kwargs.get("task_id") == self.task_ids[0]:
                return completion("content_filter", "")
            return completion("stop", "next succeeded")

        def split(chunk, temp_dir, seconds):
            return retry300 if seconds == 300 else retry150

        audio = self.make_chunk("downloaded/audio.mp3", "source")
        with (
            patch.object(app, "load_mimo_api_key", return_value="test-key"),
            patch.object(app, "mimo_audio_chunks", return_value=[original]),
            patch.object(app, "mimo_request", side_effect=request),
            patch.object(app, "split_mimo_chunk", side_effect=split),
            patch.object(app, "update_transcription", side_effect=update),
        ):
            app.run_transcription_audio(self.task_ids[0], audio)
            app.run_transcription_audio(self.task_ids[1], audio)

        first = app.TRANSCRIPTION_TASKS[self.task_ids[0]]
        second = app.TRANSCRIPTION_TASKS[self.task_ids[1]]
        self.assertEqual(first["status"], "failed")
        self.assertIn("第 1/1 段缩小至约 150 秒后仍被内容过滤", first["error"])
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(second["result"]["text"], "next succeeded")
        self.assertEqual(app.MIMO_ACTIVE_TASK_ID, "")
        self.assertEqual(app.MIMO_QUEUE, [])

    def test_mimo_queue_allows_only_one_cloud_task_at_a_time(self):
        entered_first = threading.Event()
        release_first = threading.Event()
        entered_second = threading.Event()
        guard = threading.Lock()
        active = 0
        max_active = 0

        def transcribe(_audio, _temp, _provider, _model, _progress, task_id):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            try:
                if task_id == self.task_ids[0]:
                    entered_first.set()
                    if not release_first.wait(3):
                        raise AssertionError("timed out waiting to release first task")
                else:
                    entered_second.set()
                return {"text": task_id}
            finally:
                with guard:
                    active -= 1

        def update(task_id, **fields):
            app.TRANSCRIPTION_TASKS[task_id].update(fields)
            return dict(app.TRANSCRIPTION_TASKS[task_id])

        with patch.object(app, "mimo_transcribe_audio", side_effect=transcribe), patch.object(app, "update_transcription", side_effect=update):
            first_thread = threading.Thread(
                target=app.run_transcription_audio,
                args=(self.task_ids[0], self.root / f"{self.task_ids[0]}.mp3"),
            )
            second_thread = threading.Thread(
                target=app.run_transcription_audio,
                args=(self.task_ids[1], self.root / f"{self.task_ids[1]}.mp3"),
            )
            first_thread.start()
            self.assertTrue(entered_first.wait(2))
            second_thread.start()
            threads = [
                first_thread,
                second_thread,
            ]
            time.sleep(0.1)
            self.assertFalse(entered_second.is_set())
            release_first.set()
            for thread in threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
        self.assertEqual(max_active, 1)


if __name__ == "__main__":
    unittest.main()
