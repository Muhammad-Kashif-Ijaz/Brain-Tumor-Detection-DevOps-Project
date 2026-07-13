from pathlib import Path

from flask import Flask

from .config import DefaultConfig
from .inference import BrainTumorInference
from .routes import bp


def create_app(config_override=None):
    app = Flask(__name__)
    app.config.from_object(DefaultConfig)
    if config_override:
        app.config.update(config_override)

    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["RESULT_FOLDER"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["MODEL_BUNDLE_DIR"]).mkdir(parents=True, exist_ok=True)

    app.extensions["inference_service"] = BrainTumorInference(
        result_folder=Path(app.config["RESULT_FOLDER"]),
        model_bundle_dir=Path(app.config["MODEL_BUNDLE_DIR"]),
        auto_download_model=app.config["AUTO_DOWNLOAD_MODEL"],
        max_video_frames=app.config["MAX_VIDEO_FRAMES"],
    )

    app.register_blueprint(bp)
    return app
