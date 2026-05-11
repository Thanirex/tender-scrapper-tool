#!/bin/bash
set -e
exec uvicorn api:app --host 0.0.0.0 --port 8001 --log-level info
