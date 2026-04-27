from pathlib import Path

# Directory that contains api.py, static/, agents/, etc.
APP_DIR = Path(__file__).parent

# All user data goes here — survives app moves or reinstalls
DATA_DIR = Path.home() / "Documents" / "Tender Scrapping Documents"
OUTPUTS_DIR  = DATA_DIR / "outputs"
DOWNLOADS_DIR = DATA_DIR / "downloads"


def init():
    """Create data directories on first run."""
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
