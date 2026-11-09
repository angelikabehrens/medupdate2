"""Abgleich mit dem Vorjahresmanuskript (2026-11-08): Arbeiten, die dort schon gewürdigt oder zitiert
wurden, werden in Schritt 1 nicht erneut empfohlen. Das Vorjahresmanuskript ist freiwillig; ohne Upload
läuft die Suche wie bisher (neue Gesamtthemen gibt es ohnehin nicht im Vorjahr).

Kein KI-Aufruf: verglichen werden DOI, PMID und der Titel (ohne Leer- und Satzzeichen, damit
Silbentrennung und Zeilenumbrüche aus PDFs nicht stören). Seit 2026-11-09 zählt ein Titeltreffer nur, wenn
der Nachname des Erstautors in der Nähe steht (sonst schlossen kurze oder in längeren Titeln enthaltene Titel
fremde Arbeiten aus).

`warning` erkennt, wenn versehentlich das Manuskript des aktuellen Jahrgangs hochgeladen wurde."""

from __future__ import annotations

import re
import unicodedata

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)
PMID_RE = re.compile(r"PMID:?\s*(\d{6,9})", re.I)
TITLE_KEY_LEN = 60   # Zeichen des normalisierten Titels, die im Vorjahrestext vorkommen müssen
TITLE_MIN_LEN = 25   # kürzere Titel sind zu unspezifisch für einen sicheren Abgleich
AUTHOR_WINDOW = 500  # so viele normalisierte Zeichen vor bzw. nach dem Titel darf der Erstautor stehen
WARN_MIN_MATCHES = 8  # so viele Treffer aus dem aktuellen Suchzeitraum sind für ein Vorjahresmanuskript ungewöhnlich
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
    if len(title) < TITLE_MIN_LEN:
        return ""
    key, norm = title[:TITLE_KEY_LEN], prior.get("norm", "")
    first = _first_author(rec)
    pos = norm.find(key)
    while pos >= 0:
        if not first or first in norm[max(0, pos - AUTHOR_WINDOW):pos + len(key) + AUTHOR_WINDOW]:
            return "Titel"
        pos = norm.find(key, pos + 1)
    return ""


def _first_author(rec: dict) -> str:
    """Normalisierter Nachname des Erstautors ("" ohne Autorenangabe: dann zählt der Titel allein)."""
    from .docx_tools import surname
    authors = rec.get("authors") or []
    if not authors:
        return ""
    name = _norm(surname(authors[0]))
    return name if len(name) >= 3 else ""


def warning(prior: dict | None, n_matches: int, window: list[str] | tuple | None) -> str:
    """Hinweistext, wenn das hochgeladene Manuskript vermutlich nicht das des Vorjahres ist: sehr viele der
    im aktuellen Suchzeitraum gefundenen Arbeiten stehen schon darin, oder der Dateiname nennt das Jahr, in
    dem der Suchzeitraum endet (oder ein späteres)."""
    if not prior:
        return ""
    reasons = []
    if n_matches >= WARN_MIN_MATCHES:
        reasons.append(f"{n_matches} der im aktuellen Suchzeitraum gefundenen Arbeiten stehen bereits darin")
    end_year = 0
    if window and len(window) == 2 and window[1]:
        try:
            end_year = int(str(window[1])[:4])
        except ValueError:
            end_year = 0
    years = [int(y) for n in prior.get("names", []) for y in re.findall(r"(?<!\d)(20\d\d)(?!\d)", n)]
    if end_year and years and max(years) >= end_year:
        reasons.append(f"der Dateiname nennt das Jahr {max(years)}, der Suchzeitraum endet {end_year}")
    if not reasons:
        return ""
    text = "; ".join(reasons)
    return ("Ist das wirklich das Manuskript des Vorjahres? " + text[0].upper() + text[1:]
            + ". Arbeiten aus dem Vorjahresmanuskript werden nicht empfohlen; beim aktuellen Manuskript würden "
              "genau die passenden Arbeiten fehlen.")
