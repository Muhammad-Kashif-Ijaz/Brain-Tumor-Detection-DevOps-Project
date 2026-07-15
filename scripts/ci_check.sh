#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

"$PYTHON_BIN" - <<'PY'
import ast
import re
from pathlib import Path

paths = [Path("app.py"), *Path("brain_mri_app").glob("*.py"), *Path("scripts").glob("*.py")]
for path in paths:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print(f"syntax ok: {path}")

html = Path("templates/index.html").read_text(encoding="utf-8")
javascript = Path("static/js/app.js").read_text(encoding="utf-8")
html_ids = set(re.findall(r'id="([^"]+)"', html))
javascript_ids = set(re.findall(r'getElementById\("([^"]+)"', javascript))
missing_ids = sorted(javascript_ids - html_ids)
if missing_ids:
    raise SystemExit(f"JavaScript references missing HTML ids: {', '.join(missing_ids)}")
print(f"DOM bindings ok: {len(javascript_ids)} ids")
PY

if command -v node >/dev/null 2>&1; then
  node --check static/js/app.js
  echo "syntax ok: static/js/app.js"
fi
