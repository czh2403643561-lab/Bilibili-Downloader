from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import torch
from funasr import AutoModel

from . import config

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[int, int], None]


class AudioDecodeError(RuntimeError):
    pass


class FunASREngine:
    """One resident FunASR model pipeline shared by the serial GPU worker."""

    def __init__(self) -> None:
        self.load_started_at = time.time()
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA 不可用，服务不会回退到 CPU。")
        torch.cuda.set_device(0)
        self.model = AutoModel(
            model=config.ASR_MODEL,
            vad_model=config.VAD_MODEL,
            punc_model=config.PUNC_MODEL,
            device=config.DEVICE,
            disable_update=True,
        )
        torch.cuda.synchronize()
        self.load_seconds = time.time() - self.load_started_at
        self.device = config.DEVICE

    @property
    def model_info(self) -> dict[str, Any]:
        return {
            "asr": config.ASR_MODEL,
            "asr_id": config.ASR_MODEL_ID,
            "vad": config.VAD_MODEL,
            "vad_id": config.VAD_MODEL_ID,
            "punc": config.PUNC_MODEL,
            "punc_id": config.PUNC_MODEL_ID,
            "device": self.device,
            "gpu_available": bool(torch.cuda.is_available()),
            "speaker_enabled": False,
            "hotwords_enabled": True,
        }

    @staticmethod
    def probe_duration(audio_path: Path) -> float:
        try:
            output = subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(audio_path),
                ],
                text=True,
                stderr=subprocess.STDOUT,
            ).strip()
            return float(output)
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            raise AudioDecodeError(f"无法读取音频时长：{exc}") from exc

    @staticmethod
    def normalize_audio(input_path: Path, output_path: Path) -> float:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise AudioDecodeError(f"音频格式无法解码：{detail[-1000:]}") from exc
        return FunASREngine.probe_duration(output_path)

    @staticmethod
    def _write_hotword_file(hotwords: list[str]) -> str | None:
        if not hotwords:
            return None
        fd, path = tempfile.mkstemp(prefix="funasr-hotwords-", suffix=".txt")
        os.close(fd)
        try:
            # FunASR 1.4.16's Windows hotword reader uses the system GBK codec.
            Path(path).write_bytes(("\n".join(hotwords) + "\n").encode("gbk"))
        except UnicodeEncodeError as exc:
            Path(path).unlink(missing_ok=True)
            raise ValueError("热词包含当前 Windows GBK 无法编码的字符。") from exc
        return path

    @staticmethod
    def _segments(result: dict[str, Any]) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        for item in result.get("sentence_info") or []:
            try:
                start = int(item["start"])
                end = int(item["end"])
                text = str(item.get("text", item.get("sentence", ""))).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if not text or end < start:
                continue
            segments.append(
                {
                    "start_ms": start,
                    "end_ms": end,
                    "text": text,
                    "speaker": item.get("spk"),
                }
            )
        return segments

    def transcribe(
        self,
        audio_path: Path,
        hotwords: list[str],
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        duration = self.probe_duration(audio_path)
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats(0)
        hotword_path = self._write_hotword_file(hotwords)
        started = time.perf_counter()

        def callback(current: int, total: int) -> None:
            if progress_callback is not None:
                progress_callback(current, total)

        try:
            kwargs: dict[str, Any] = {
                "input": str(audio_path),
                "batch_size_s": config.ASR_BATCH_SIZE_S,
                "sentence_timestamp": True,
                "return_raw_text": True,
                "disable_pbar": True,
                "progress_callback": callback,
            }
            if hotword_path:
                kwargs["hotword"] = hotword_path
            result_list = self.model.generate(**kwargs)
            torch.cuda.synchronize()
        finally:
            if hotword_path:
                Path(hotword_path).unlink(missing_ok=True)

        inference_seconds = time.perf_counter() - started
        if not result_list:
            raise RuntimeError("FunASR 返回空结果。")
        result = result_list[0]
        timestamps = result.get("timestamp", result.get("timestamps", [])) or []
        peak_allocated = torch.cuda.max_memory_allocated(0) / 1024**2
        peak_reserved = torch.cuda.max_memory_reserved(0) / 1024**2
        return {
            "text": str(result.get("text", "")),
            "segments": self._segments(result),
            "timestamps": timestamps,
            "model": {**self.model_info, "hotwords_enabled": bool(hotwords)},
            "metrics": {
                "audio_seconds": duration,
                "model_load_seconds": self.load_seconds,
                "inference_seconds": inference_seconds,
                "rtf": duration / inference_seconds if inference_seconds else None,
                "peak_allocated_mib": peak_allocated,
                "peak_reserved_mib": peak_reserved,
            },
        }
