import os
from pathlib import Path

# Directory that contains api.py, static/, agents/, etc.
APP_DIR = Path(__file__).parent

# TENDER_DATA_DIR env var lets Docker (or CI) redirect data to a mounted volume.
# Falls back to the original Windows user-documents path for local installs.
_data_root = os.getenv("TENDER_DATA_DIR")
DATA_DIR = Path(_data_root) if _data_root else Path.home() / "Documents" / "Tender Scrapping Documents"
OUTPUTS_DIR   = DATA_DIR / "outputs"
DOWNLOADS_DIR = DATA_DIR / "downloads"
CRON_LOGS_DIR = DATA_DIR / "cron_logs"
DB_PATH       = DATA_DIR / "tender_tracker.db"


def init():
    """Create data directories on first run."""
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    CRON_LOGS_DIR.mkdir(parents=True, exist_ok=True)
