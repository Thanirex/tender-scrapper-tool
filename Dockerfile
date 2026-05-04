FROM python:3.12-slim

WORKDIR /app

# Virtual display + VNC + noVNC for headed browser support
RUN apt-get update && apt-get install -y \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    x11-utils \
    && rm -rf /var/lib/apt/lists/*

# pip deps first (layer cached separately from code)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium + all its Linux system deps
RUN playwright install --with-deps chromium

COPY . .

# Make the start script executable
RUN chmod +x entrypoint.sh

# :99 virtual display is always available; apps can launch headed Chromium
ENV DISPLAY=:99

EXPOSE 8000 6080

CMD ["./entrypoint.sh"]
