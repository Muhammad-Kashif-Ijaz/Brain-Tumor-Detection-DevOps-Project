from pathlib import Path

from flask import Flask, request

from .config import BASE_DIR, DefaultConfig
from .inference import BrainTumorInference
from .routes import bp


def create_app(config_override=None):
    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "templates"),
        static_folder=str(BASE_DIR / "static"),
    )
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

    @app.after_request
    def add_no_cache_headers(response):
        if request.path == "/" or request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    app.register_blueprint(bp)
    return app
