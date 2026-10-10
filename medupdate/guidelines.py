"""Leitlinien: per Websuche finden, frei verfügbare PDFs laden, relevante Auszüge bilden."""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin

import requests

from . import prompts_new as P
from .config import MODEL_FAST
from .files import download_pdf, extract_pdf_text
from .http import BROWSER_HEADERS, request_with_retry
from .llm import BOOL, LLM, STR, arr, obj

GUIDELINE_SCHEMA = obj({
    "guidelines": arr(obj({
        "title": STR,
        "society": STR,
        "year": STR,
        "register_number": STR,
        "page_url": STR,
        "pdf_url": STR,
        "freely_available": BOOL,
        "relevance": STR,
    })),
})


def search_guidelines(llm: LLM, topic: str, chapter: str) -> list[dict]:
    research = llm.text(P.guideline_search_prompt(topic, chapter, date.today().strftime("%d.%m.%Y")),
                        model=MODEL_FAST, web_search=8, max_tokens=12000, effort="medium")
    data = llm.json(P.guideline_extract_prompt(research), GUIDELINE_SCHEMA, max_tokens=6000)
    return data.get("guidelines", [])


_PDF_LINK_RE = re.compile(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', re.IGNORECASE)


def _pdf_links_from_page(url: str) -> list[str]:
    try:
        r = request_with_retry("GET", url, timeout=30, headers=BROWSER_HEADERS, retries=2)
    except requests.exceptions.RequestException:
        return []
    if r.content[:4] == b"%PDF":
        return [url]
    links = [urljoin(url, m) for m in _PDF_LINK_RE.findall(r.text)]
    # Langfassungen zuerst (AWMF: "..._S3_..." ohne "Kurzfassung"/"Leitlinienreport")
    def rank(link: str) -> int:
        low = link.lower()
        bad = any(w in low for w in ("kurzfassung", "leitlinienreport", "report", "patient", "evidenz"))
        return (1 if bad else 0)
    return sorted(dict.fromkeys(links), key=rank)[:4]


def fetch_guideline_text(g: dict) -> tuple[str, str]:
    """Versucht, den Volltext einer Leitlinie zu laden. Gibt (Text, Quelle-URL) oder ("", "") zurück."""
    candidates = []
    if g.get("pdf_url"):
        candidates.append(g["pdf_url"])
    if g.get("page_url"):
        candidates.extend(_pdf_links_from_page(g["page_url"]))
    for url in dict.fromkeys(candidates):
        pdf = download_pdf(url)
        if pdf:
            text = extract_pdf_text(pdf)
            if len(text) > 3000:
                return text, url
    return "", ""


# ---------- Relevante Auszüge (statt ganzer Leitlinien mit mehreren hundert Seiten) ----------

_WORD_RE = re.compile(r"[a-zäöüß0-9-]{4,}", re.IGNORECASE)
_STOP = {"with", "from", "that", "this", "were", "have", "been", "patients", "patienten", "study", "studie",
         "sowie", "werden", "wurde", "nach", "eine", "einer", "oder", "nicht", "auch", "durch", "sollte",
         "kann", "sind", "bei", "und", "der", "die", "das"}


def _terms(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text)} - _STOP


def relevant_excerpt(text: str, query: str, max_chars: int = 15000, chunk: int = 2500) -> str:
    """Wählt die Abschnitte einer langen Leitlinie aus, die die meisten Suchbegriffe enthalten
    (Empfehlungskästen werden leicht bevorzugt) und gibt sie in Originalreihenfolge zurück."""
    if len(text) <= max_chars:
        return text
    q = _terms(query)
    pieces = [text[i:i + chunk] for i in range(0, len(text), chunk)]
    scored = []
    for idx, piece in enumerate(pieces):
        t = _terms(piece)
        s = len(q & t)
        if re.search(r"empfehlung|recommendation|statement|konsens", piece, re.IGNORECASE):
            s += 2
        scored.append((s, idx))
    keep = sorted(idx for _, idx in sorted(scored, reverse=True)[: max(1, max_chars // chunk)])
    return "\n[…]\n".join(pieces[i] for i in keep)
