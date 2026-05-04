#!/bin/bash
set -e

# Start virtual framebuffer display on :99
Xvfb :99 -screen 0 1280x900x24 &
sleep 1

# VNC server — no password, background mode
x11vnc -display :99 -forever -nopw -quiet -bg

# noVNC web proxy: browser view at http://localhost:6080/vnc.html
websockify --web=/usr/share/novnc/ 6080 localhost:5900 &

# Start the FastAPI app
exec uvicorn api:app --host 0.0.0.0 --port 8000
