from threading import Lock
from time import time


_lock = Lock()
_started_at = time()
_metrics = {
    "requests_total": 0,
    "analysis_total": 0,
    "analysis_errors_total": 0,
    "analysis_seconds_sum": 0.0,
}


def record_request():
    with _lock:
        _metrics["requests_total"] += 1


def record_analysis(seconds, ok=True):
    with _lock:
        _metrics["analysis_total"] += 1
        _metrics["analysis_seconds_sum"] += float(seconds)
        if not ok:
            _metrics["analysis_errors_total"] += 1


def render_metrics():
    with _lock:
        snapshot = dict(_metrics)
    uptime = time() - _started_at
    avg = 0.0
    if snapshot["analysis_total"]:
        avg = snapshot["analysis_seconds_sum"] / snapshot["analysis_total"]
    lines = [
        "# HELP brain_mri_app_uptime_seconds Application uptime in seconds.",
        "# TYPE brain_mri_app_uptime_seconds gauge",
        f"brain_mri_app_uptime_seconds {uptime:.3f}",
        "# HELP brain_mri_requests_total Total HTTP requests seen by the app.",
        "# TYPE brain_mri_requests_total counter",
        f"brain_mri_requests_total {snapshot['requests_total']}",
        "# HELP brain_mri_analysis_total Total analysis attempts.",
        "# TYPE brain_mri_analysis_total counter",
        f"brain_mri_analysis_total {snapshot['analysis_total']}",
        "# HELP brain_mri_analysis_errors_total Total failed analysis attempts.",
        "# TYPE brain_mri_analysis_errors_total counter",
        f"brain_mri_analysis_errors_total {snapshot['analysis_errors_total']}",
        "# HELP brain_mri_analysis_seconds_average Average analysis duration.",
        "# TYPE brain_mri_analysis_seconds_average gauge",
        f"brain_mri_analysis_seconds_average {avg:.3f}",
    ]
    return "\n".join(lines) + "\n"
