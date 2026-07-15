import os
import time
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = Path(os.getenv("APP_STORAGE_DIR", BASE_DIR / "instance"))


class DefaultConfig:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_MB", "512")) * 1024 * 1024

    UPLOAD_FOLDER = Path(os.getenv("UPLOAD_FOLDER", INSTANCE_DIR / "uploads"))
    RESULT_FOLDER = Path(os.getenv("RESULT_FOLDER", INSTANCE_DIR / "results"))
    MODEL_BUNDLE_DIR = Path(os.getenv("MODEL_BUNDLE_DIR", INSTANCE_DIR / "models"))
    AUTO_DOWNLOAD_MODEL = os.getenv("AUTO_DOWNLOAD_MODEL", "false").lower() == "true"
    MAX_VIDEO_FRAMES = int(os.getenv("MAX_VIDEO_FRAMES", "10"))
    ASSET_VERSION = os.getenv("ASSET_VERSION", str(int(time.time())))
