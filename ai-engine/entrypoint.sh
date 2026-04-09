#!/bin/sh
# Auto-install any packages in requirements.txt missing from the image.
# This runs on every container start and is a no-op if already installed.
pip install --no-cache-dir -r /app/requirements.txt -q
exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
