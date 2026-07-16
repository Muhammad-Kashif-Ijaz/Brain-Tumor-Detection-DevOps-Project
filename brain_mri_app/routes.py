import time
import uuid
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from . import metrics


bp = Blueprint("mri", __name__)


@bp.before_app_request
def before_request():
    metrics.record_request()


@bp.get("/")
def index():
    return render_template("index.html", asset_version=current_app.config["ASSET_VERSION"])


@bp.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@bp.get("/readyz")
def readyz():
    root_path = Path(current_app.root_path).parent
    service = current_app.extensions["inference_service"]
    slice_model_ready = service.slice_model_ready()
    payload = {
        "status": "ok" if slice_model_ready else "unavailable",
        "template_found": (root_path / "templates" / "index.html").exists(),
        "static_found": (root_path / "static").exists(),
        "upload_storage_ready": Path(current_app.config["UPLOAD_FOLDER"]).exists(),
        "result_storage_ready": Path(current_app.config["RESULT_FOLDER"]).exists(),
        "asset_version": current_app.config["ASSET_VERSION"],
        "slice_model_ready": slice_model_ready,
        "volume_model_ready": service._monai_bundle_ready(),
    }
    return jsonify(payload), 200 if slice_model_ready else 503


@bp.get("/metrics")
def prometheus_metrics():
    return Response(metrics.render_metrics(), mimetype="text/plain; version=0.0.4")


@bp.get("/results/<path:filename>")
def result_file(filename):
    return send_from_directory(current_app.config["RESULT_FOLDER"], filename)


@bp.post("/api/analyze")
def analyze_upload():
    started = time.time()
    service = current_app.extensions["inference_service"]
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
    saved_paths = []
    try:
        modality_paths = _save_modalities(upload_dir)
        saved_paths.extend(modality_paths.values())
        if modality_paths:
            result = service.analyze_multimodal_nifti(modality_paths)
        else:
            upload = request.files.get("scan")
            if not upload or not upload.filename:
                return jsonify({"status": "error", "message": "Choose an MRI image, video, or NIfTI files."}), 400
            saved_path = _save_upload(upload, upload_dir)
            saved_paths.append(saved_path)
            result = service.analyze_file(saved_path)
        metrics.record_analysis(time.time() - started, ok=result.status == "ok")
        status_code = 200 if result.status == "ok" else 422
        return jsonify(_with_result_urls(result.to_dict())), status_code
    except Exception:
        metrics.record_analysis(time.time() - started, ok=False)
        return jsonify({"status": "error", "message": "The study could not be processed. Please check the files and try again."}), 500
    finally:
        for path in saved_paths:
            path.unlink(missing_ok=True)


@bp.post("/api/live-frame")
def analyze_live_frame():
    started = time.time()
    service = current_app.extensions["inference_service"]
    payload = request.get_json(silent=True) or {}
    frame = payload.get("frame")
    if not frame:
        return jsonify({"status": "error", "message": "Missing live frame."}), 400
    result = service.analyze_live_frame(frame)
    metrics.record_analysis(time.time() - started, ok=result.status == "ok")
    status_code = 200 if result.status == "ok" else 422
    return jsonify(_with_result_urls(result.to_dict())), status_code


def _save_upload(file_storage, upload_dir: Path) -> Path:
    filename = secure_filename(file_storage.filename or "scan")
    saved_name = f"{uuid.uuid4().hex}-{filename}"
    saved_path = upload_dir / saved_name
    file_storage.save(saved_path)
    return saved_path


def _save_modalities(upload_dir: Path):
    modality_paths = {}
    for modality in ("t1c", "t1", "t2", "flair"):
        upload = request.files.get(modality)
        if upload and upload.filename:
            modality_paths[modality] = _save_upload(upload, upload_dir)
    return modality_paths


def _with_result_urls(payload):
    source_filename = payload.get("source_preview_filename")
    if source_filename:
        payload["source_preview_url"] = url_for("mri.result_file", filename=source_filename)
    filename = payload.get("overlay_filename")
    if filename:
        payload["overlay_url"] = url_for("mri.result_file", filename=filename)
    return payload
