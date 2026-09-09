import time
from pathlib import Path
import pdfplumber
from docx import Document

# Extraction budgets.
#
# Every caller truncates this function's output to 25,000 characters before
# sending it to the summariser, so parsing a 300-page tender PDF in full threw
# away ~97% of the work. pdfplumber's extract_tables() is the expensive half
# and turns pathological on large scanned or vector-heavy documents — which is
# how a daily run could sit on one site for hours. Nothing here logs, so the
# runner's cooperative stop check never got a chance to fire either.
#
# These caps stop at the point the caller would have truncated anyway, so the
# text handed to the LLM is unchanged for any document that mattered.
_MAX_TEXT_CHARS = 30_000   # a margin above the callers' 25,000-char cut
_MAX_PDF_PAGES  = 150      # hard stop for documents with no useful text
_MAX_PDF_SECONDS = 90      # wall-clock budget for one file


def read_file(path: str) -> str:
    """
    Extract text from a downloaded tender file.
    Supported: PDF, DOCX, XLSX, TXT.
    Returns empty string on unsupported type or failure.
    """
    ext = Path(path).suffix.lower()
    try:
        if ext == ".pdf":
            return _read_pdf(path)
        elif ext in (".docx", ".doc"):
            return _read_docx(path)
        elif ext in (".xlsx", ".xls"):
            return _read_excel(path)
        elif ext == ".txt":
            with open(path, encoding="utf-8", errors="ignore") as f:
                return f.read()
        else:
            return ""
    except Exception as e:
        return f"[Could not read {Path(path).name}: {e}]"


def _read_pdf(path: str) -> str:
    """Extract text and tables, stopping at the budgets above.

    Pages are processed in order, so the caps only ever drop content the
    caller was going to truncate away.
    """
    parts, size = [], 0
    started = time.monotonic()
    stopped_early = False

    with pdfplumber.open(path) as pdf:
        for page_no, pg in enumerate(pdf.pages, 1):
            if (size >= _MAX_TEXT_CHARS
                    or page_no > _MAX_PDF_PAGES
                    or time.monotonic() - started > _MAX_PDF_SECONDS):
                stopped_early = True
                break

            # Regular text
            try:
                text = pg.extract_text()
            except Exception:
                text = None
            if text:
                parts.append(text)
                size += len(text)

            # Tables — eligibility criteria and budget are often in tables.
            # Skipped once the text budget is met: this is the slow call, and
            # anything it returns past that point is discarded anyway.
            if size >= _MAX_TEXT_CHARS:
                stopped_early = True
                break
            try:
                for table in pg.extract_tables():
                    for row in table:
                        row_str = " | ".join(str(c or "").strip() for c in row if c)
                        if row_str.strip():
                            line = f"[TABLE] {row_str}"
                            parts.append(line)
                            size += len(line)
            except Exception:
                pass

    if stopped_early:
        parts.append(
            f"[Extraction stopped early — {Path(path).name} exceeded the "
            f"per-file page/time/size budget. Earlier pages are included in full.]"
        )
    return "\n".join(parts)


def _read_docx(path: str) -> str:
    doc = Document(path)
    parts = []

    from docx.text.paragraph import Paragraph
    from docx.table import Table

    # Walk the document body in order: paragraphs and tables are interleaved
    for block in doc.element.body:
        tag = block.tag.split("}")[-1] if "}" in block.tag else block.tag
        if tag == "p":
            para = Paragraph(block, doc)
            if para.text.strip():
                parts.append(para.text.strip())
        elif tag == "tbl":
            tbl = Table(block, doc)
            for row in tbl.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    parts.append(f"[TABLE] {row_text}")

    return "\n".join(parts)


def _read_excel(path: str) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    parts = []
    for sheet in wb.worksheets:
        parts.append(f"[Sheet: {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            row_text = "  |  ".join(str(c) for c in row if c is not None)
            if row_text.strip():
                parts.append(row_text)
    wb.close()
    return "\n".join(parts)
