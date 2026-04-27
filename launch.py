"""
Double-click launcher for Tender Scrapper.
Starts the FastAPI server then opens the browser automatically.
"""
import os
import sys
import time
import subprocess
import webbrowser
import urllib.request
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8000
URL  = f"http://{HOST}:{PORT}"

# Always run from the directory this file lives in
os.chdir(Path(__file__).parent)


def _server_ready() -> bool:
    try:
        urllib.request.urlopen(URL, timeout=1)
        return True
    except Exception:
        return False


def main():
    print("=" * 50)
    print("  Tender Scrapper")
    print(f"  Starting server on {URL}")
    print("  Press Ctrl+C to stop.")
    print("=" * 50)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api:app",
         "--host", HOST, "--port", str(PORT)],
        cwd=str(Path(__file__).parent),
    )

    # Wait up to 15 s for the server to accept connections
    print("\nWaiting for server to start", end="", flush=True)
    for _ in range(30):
        if _server_ready():
            break
        time.sleep(0.5)
        print(".", end="", flush=True)
    print()

    if _server_ready():
        print(f"Server ready — opening {URL}")
        webbrowser.open(URL)
    else:
        print("Server didn't start in time. Open your browser manually at", URL)

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        proc.terminate()


if __name__ == "__main__":
    main()
