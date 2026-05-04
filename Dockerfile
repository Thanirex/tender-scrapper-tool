FROM python:3.12-slim

WORKDIR /app

# pip deps first (layer cached separately from code)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium + all its Linux system deps
RUN playwright install --with-deps chromium

COPY . .

# Make the start script executable
RUN chmod +x entrypoint.sh

EXPOSE 8000

CMD ["./entrypoint.sh"]
