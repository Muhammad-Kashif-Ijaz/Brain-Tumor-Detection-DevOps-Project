import logging
import os
from pathlib import Path

import flask


LOGGER = logging.getLogger(__name__)


def _configure_azure_monitor():
    """Enable request telemetry only when Azure injects an App Insights connection string."""
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not connection_string:
        return

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(connection_string=connection_string)
    except Exception:
        # Telemetry must never prevent MRI review traffic from starting.
        LOGGER.exception("Azure Monitor telemetry could not be configured.")


_configure_azure_monitor()

from flask import request

from .config import BASE_DIR, DefaultConfig
from .inference import BrainTumorInference
from .routes import bp


def create_app(config_override=None):
    app = flask.Flask(
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
