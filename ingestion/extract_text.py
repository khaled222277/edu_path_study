"""
Unified PDF text extraction for EduPath Study V2 ingestion.

Handles mixed PDFs where some pages have real text and others are
scanned images — decides per PAGE, not per file, whether native
extraction or OCR is needed.

Dependencies:
    pip install pymupdf pytesseract pillow --break-system-packages

System requirement (OCR fallback needs Tesseract itself, not just the
Python wrapper):
    Ubuntu/Debian: sudo apt-get install tesseract-ocr tesseract-ocr-ara
    (tesseract-ocr-ara = Arabic language data; without it, Arabic OCR
    will silently produce garbage or fail)
"""

from __future__ import annotations

import io
import logging

import fitz  # PyMuPDF
import pytesseract
from PIL import Image

logger = logging.getLogger(__name__)

MIN_TEXT_CHARS_PER_PAGE = 40
DEFAULT_OCR_LANGS = "ara+eng"
DEFAULT_OCR_DPI = 300


def extract_pdf_pages(path, ocr_langs=DEFAULT_OCR_LANGS, ocr_dpi=DEFAULT_OCR_DPI, min_text_chars=MIN_TEXT_CHARS_PER_PAGE):
    doc = fitz.open(path)
    pages_out = []
    try:
        for i in range(len(doc)):
            page = doc[i]
            native_text = page.get_text().strip()
            if len(native_text) >= min_text_chars:
                pages_out.append({"page": i + 1, "text": native_text, "method": "native"})
                continue
            try:
                pix = page.get_pixmap(dpi=ocr_dpi)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                ocr_text = pytesseract.image_to_string(img, lang=ocr_langs).strip()
                pages_out.append({"page": i + 1, "text": ocr_text, "method": "ocr"})
            except Exception as e:
                logger.warning("OCR failed on %s page %d: %s", path, i + 1, e)
                pages_out.append({"page": i + 1, "text": "", "method": "ocr_failed"})
    finally:
        doc.close()
    return pages_out


def extract_pdf_text(path, **kwargs):
    pages = extract_pdf_pages(path, **kwargs)
    return "\n\n".join(f"[Page {p['page']}]\n{p['text']}" for p in pages if p["text"])


def extraction_report(path, **kwargs):
    pages = extract_pdf_pages(path, **kwargs)
    total = len(pages)
    ocr_pages = sum(1 for p in pages if p["method"] == "ocr")
    failed_pages = sum(1 for p in pages if p["method"] == "ocr_failed")
    empty_pages = sum(1 for p in pages if not p["text"].strip())
    return {
        "file": path,
        "total_pages": total,
        "native_pages": total - ocr_pages - failed_pages,
        "ocr_pages": ocr_pages,
        "ocr_failed_pages": failed_pages,
        "empty_pages": empty_pages,
    }


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("Usage: python extract_text.py <path-to-pdf>")
        sys.exit(1)
    report = extraction_report(sys.argv[1])
    print(json.dumps(report, indent=2, ensure_ascii=False))