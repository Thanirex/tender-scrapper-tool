from pathlib import Path
import pdfplumber
from docx import Document


def read_file(path: str) -> str:
    """Extract text from PDF, DOCX, or TXT. Returns empty string on failure."""
    ext = Path(path).suffix.lower()
    try:
        if ext == ".pdf":
            return _read_pdf(path)
        elif ext in (".docx", ".doc"):
            return _read_docx(path)
        elif ext == ".txt":
            with open(path, encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception as e:
        return f"[Could not read {Path(path).name}: {e}]"
    return ""


def _read_pdf(path: str) -> str:
    parts = []
    with pdfplumber.open(path) as pdf:
        for pg in pdf.pages:
            text = pg.extract_text()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _read_docx(path: str) -> str:
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
