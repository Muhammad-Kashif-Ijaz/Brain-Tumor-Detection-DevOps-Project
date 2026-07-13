#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

"$PYTHON_BIN" - <<'PY'
import ast
from pathlib import Path

for path in [Path("app.py"), *Path("brain_mri_app").glob("*.py")]:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print(f"syntax ok: {path}")
PY
