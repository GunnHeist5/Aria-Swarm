"""Ingest channel 1: document upload -> draft method.

PDF text extraction is real (pypdf). pptx decks are accepted and stored but
flagged needs_manual_extraction — the trainer pastes the content in the review
screen. Ingestion is open; nothing here is retrievable until the review gate
approves it (learn/route.py contract)."""

from __future__ import annotations

import io
from pathlib import Path

from app.db import knowledge


def extract_text(filename: str, data: bytes) -> tuple[str, bool]:
    """Returns (text, needs_manual_extraction)."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
        return text.strip(), False
    if suffix in (".md", ".txt"):
        return data.decode(errors="replace").strip(), False
    # pptx/docx/etc: stored as a stub draft, extracted manually at review time.
    return "", True


def ingest_document(filename: str, data: bytes, domain: str | None = None) -> str:
    text, needs_manual = extract_text(filename, data)
    if needs_manual:
        body = f"*(needs_manual_extraction: paste the content of `{filename}` here during review)*"
    else:
        body = text or "*(no extractable text found)*"
    return knowledge.create_draft(
        kind="framework",
        title=Path(filename).stem.replace("_", " ").replace("-", " ").strip() or filename,
        body_md=body,
        domain=domain,
        source_kind="deck_upload",
        source_ref=filename,
    )
