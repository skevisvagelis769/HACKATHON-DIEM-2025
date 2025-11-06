#!/usr/bin/env bash
set -euo pipefail

# Dev runner for the FastAPI app
export PYTHONUNBUFFERED=1

# Change to this script's directory (so it works from anywhere)
cd "$(dirname "$0")"

# Run Uvicorn with hot reload on port 8000
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
source .venv/bin/activate
cd .. 
cd frontend/
python -m http.server 5173