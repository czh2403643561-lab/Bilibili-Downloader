from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.getenv("ASR_DATA_DIR", str(PROJECT_ROOT / "data"))).resolve()
JOBS_ROOT = DATA_ROOT / "jobs"
DB_PATH = Path(os.getenv("ASR_DB_PATH", str(DATA_ROOT / "asr_jobs.sqlite3"))).resolve()

ASR_MODEL = os.getenv("ASR_MODEL", "SeacoParaformer")
ASR_MODEL_ID = os.getenv(
    "ASR_MODEL_ID",
    "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
)
VAD_MODEL = os.getenv("VAD_MODEL", "fsmn-vad")
VAD_MODEL_ID = os.getenv(
    "VAD_MODEL_ID", "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
)
PUNC_MODEL = os.getenv("PUNC_MODEL", "ct-punc")
PUNC_MODEL_ID = os.getenv(
    "PUNC_MODEL_ID", "iic/punc_ct-transformer_cn-en-common-vocab471067-large"
)
DEVICE = os.getenv("ASR_DEVICE", "cuda:0")

API_VERSION = "v1"
SERVICE_VERSION = "0.1.0"
MAX_UPLOAD_BYTES = int(os.getenv("ASR_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
QUEUE_MAX_SIZE = int(os.getenv("ASR_QUEUE_MAX_SIZE", "32"))
ASR_BATCH_SIZE_S = int(os.getenv("ASR_BATCH_SIZE_S", "300"))

ALLOWED_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".mp4",
    ".flac",
    ".ogg",
    ".opus",
    ".aac",
    ".wma",
    ".webm",
}


def cors_origins() -> list[str]:
    value = os.getenv(
        "ASR_CORS_ORIGINS",
        "http://localhost:23666,http://127.0.0.1:23666",
    )
    return [item.strip() for item in value.split(",") if item.strip()]


def cached_model_source(model_id: str) -> str:
    """优先使用已存在的 ModelScope 快照，避免本机代理配置影响启动。"""
    cache_root = Path(
        os.getenv("MODELSCOPE_CACHE", str(Path.home() / ".cache" / "modelscope" / "models"))
    )
    snapshot = cache_root / model_id.replace("/", "--") / "snapshots" / "master"
    return str(snapshot) if snapshot.is_dir() else model_id
