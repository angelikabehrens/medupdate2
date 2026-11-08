"""Abgleich mit dem Vorjahresmanuskript (2026-11-08): Arbeiten, die dort schon gewürdigt oder zitiert
wurden, werden in Schritt 1 nicht erneut empfohlen. Das Vorjahresmanuskript ist freiwillig; ohne Upload
läuft die Suche wie bisher (neue Gesamtthemen gibt es ohnehin nicht im Vorjahr).

Kein KI-Aufruf: verglichen werden DOI, PMID und der Titel (ohne Leer- und Satzzeichen, damit
Silbentrennung und Zeilenumbrüche aus PDFs nicht stören)."""

from __future__ import annotations

import re
import unicodedata

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)
PMID_RE = re.compile(r"PMID:?\s*(\d{6,9})", re.I)
TITLE_KEY_LEN = 60   # Zeichen des normalisierten Titels, die im Vorjahrestext vorkommen müssen
TITLE_MIN_LEN = 25   # kürzere Titel sind zu unspezifisch für einen sicheren Abgleich
MAX_TEXT = 1_500_000


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(ch for ch in text if ch.isalnum())


def _clean_doi(doi: str) -> str:
    return (doi or "").strip().lower().rstrip(".,;)]")


def build_prior(files: dict[str, str]) -> dict:
    """files: {Dateiname: Text}. Ergebnis wird in proj["prior"] gespeichert."""
    text = "\n".join(files.values())
    dois = sorted({_clean_doi(d) for d in DOI_RE.findall(text)})
    pmids = sorted(set(PMID_RE.findall(text)))
    return {"names": list(files), "dois": dois, "pmids": pmids, "norm": _norm(text)[:MAX_TEXT],
            "n_chars": len(text)}


def match(rec: dict, prior: dict | None) -> str:
    """Grund des Treffers ("DOI", "PMID", "Titel") oder "" ohne Treffer."""
    if not prior or not rec:
        return ""
    doi = _clean_doi(rec.get("doi", ""))
    if doi and doi in set(prior.get("dois", [])):
        return "DOI"
    if rec.get("pmid") and str(rec["pmid"]) in set(prior.get("pmids", [])):
        return "PMID"
    title = _norm(rec.get("title", ""))
    if len(title) >= TITLE_MIN_LEN and title[:TITLE_KEY_LEN] in prior.get("norm", ""):
        return "Titel"
    return ""
