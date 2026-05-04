#!/bin/bash
set -e

# Start virtual framebuffer display on :99
Xvfb :99 -screen 0 1280x900x24 &

# Wait for Xvfb to be ready instead of blind sleep
echo "Waiting for Xvfb..."
for i in $(seq 1 20); do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
        echo "[OK] Xvfb ready on :99"
        break
    fi
    if [ "$i" -eq 20 ]; then
        echo "[WARN] Xvfb did not respond in 10s, continuing anyway"
    fi
    sleep 0.5
done

# VNC server — no password, background mode
x11vnc -display :99 -forever -nopw -quiet -bg || true

# noVNC web proxy: browser view at http://localhost:6080/vnc.html
websockify --web=/usr/share/novnc/ 6080 localhost:5900 &

# Start the FastAPI app
exec uvicorn api:app --host 0.0.0.0 --port 8000
