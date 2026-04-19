#!/bin/bash
# Start just uvicorn (for daily use when already authenticated)
# Use start.sh when you need to re-connect Yahoo (OAuth)
cd "$(dirname "$0")"
echo "Starting app at http://localhost:8000"
uvicorn api.main:app --reload --port 8000
