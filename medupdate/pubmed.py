"""PubMed-Zugriff (E-Utilities). Grundfunktionen aus MyLuo, erweitert um vollständige
bibliografische Angaben (Band, Heft, Seiten, Autoren mit Initialen), damit das
Literaturverzeichnis aus echten Metadaten statt aus KI-Text gebaut werden kann."""

from __future__ import annotations

import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date

from .http import request_with_retry

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# NCBI erlaubt ohne eigenen API-Schlüssel ca. 3 Anfragen pro Sekunde – gilt auch bei parallelen Threads.
_rate_lock = threading.Lock()
_last_call = [0.0]


def _throttle() -> None:
    with _rate_lock:
        wait = 0.4 - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def _params(email: str, **extra) -> dict:
    p = {"db": "pubmed", "tool": "medupdate2", **extra}
    if email:
        p["email"] = email
    return p


def date_filter(von: date, bis: date) -> str:
    return f'("{von.strftime("%Y/%m/%d")}"[Date - Publication] : "{bis.strftime("%Y/%m/%d")}"[Date - Publication])'


def search(query: str, retmax: int, email: str = "") -> tuple[list[str], int]:
    """Gibt (PMIDs in PubMed-Relevanzreihenfolge, Gesamtzahl der Treffer) zurück."""
    _throttle()
    r = request_with_retry("GET", f"{EUTILS}/esearch.fcgi",
                           params=_params(email, term=query, retmax=retmax, retmode="xml", sort="relevance"),
                           timeout=30)
    root = ET.fromstring(r.text)
    count_el = root.find("Count")
    count = int(count_el.text) if count_el is not None and count_el.text else 0
    return [el.text for el in root.findall(".//IdList/Id")], count


def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _parse_article(article) -> dict | None:
    pmid = _text(article.find(".//MedlineCitation/PMID"))
    if not pmid:
        return None
    art = article.find(".//Article")
    title = _text(art.find("ArticleTitle")) if art is not None else ""

    abstract_parts = []
    for ab in article.findall(".//Abstract/AbstractText"):
        label = ab.get("Label")
        txt = _text(ab)
        if txt:
            abstract_parts.append(f"{label}: {txt}" if label else txt)
    abstract = "\n\n".join(abstract_parts)

    authors, authors_full = [], []
    for a in article.findall(".//AuthorList/Author"):
        last = _text(a.find("LastName"))
        initials = _text(a.find("Initials"))
        collective = _text(a.find("CollectiveName"))
        if last:
            authors.append(f"{last} {initials}".strip())
            authors_full.append(f"{last} {initials}".strip())
        elif collective:
            authors.append(collective)
            authors_full.append(collective)

    journal_iso = _text(article.find(".//Journal/ISOAbbreviation"))
    journal_full = _text(article.find(".//Journal/Title"))
    issue_el = article.find(".//Journal/JournalIssue")
    volume = _text(issue_el.find("Volume")) if issue_el is not None else ""
    issue = _text(issue_el.find("Issue")) if issue_el is not None else ""
    year = ""
    if issue_el is not None:
        year = _text(issue_el.find("PubDate/Year"))
        if not year:
            m = re.search(r"(19|20)\d{2}", _text(issue_el.find("PubDate/MedlineDate")))
            year = m.group(0) if m else ""
    if not year:
        year = _text(article.find(".//ArticleDate/Year"))
    pages = _text(article.find(".//Pagination/MedlinePgn"))

    doi = ""
    for id_el in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if id_el.get("IdType") == "doi":
            doi = _text(id_el)
            break
    if not doi:
        for eloc in article.findall(".//ELocationID"):
            if eloc.get("EIdType") == "doi":
                doi = _text(eloc)
                break
    pmc = ""
    for id_el in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if id_el.get("IdType") == "pmc":
            pmc = _text(id_el)
            break

    pub_types = [_text(pt) for pt in article.findall(".//PublicationTypeList/PublicationType")]
    langs = [_text(l) for l in article.findall(".//Article/Language")]
    return {
        "pmid": pmid,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "journal": journal_iso or journal_full,
        "journal_full": journal_full,
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "pmc": pmc,
        "pub_types": pub_types or ["Sonstige"],
        "language": langs[0] if langs else "eng",
    }


def fetch_records(pmids: list[str], email: str = "") -> dict[str, dict]:
    """Vollständige Datensätze (inkl. Abstract und bibliografischer Angaben) für PMIDs."""
    out: dict[str, dict] = {}
    for i in range(0, len(pmids), 50):  # kleinere Päckchen: weniger anfällig für 502-Fehler bei PubMed
        batch = pmids[i:i + 50]
        _throttle()
        r = request_with_retry("POST", f"{EUTILS}/efetch.fcgi",
                               data=_params(email, id=",".join(batch), rettype="abstract", retmode="xml"),
                               timeout=60)
        root = ET.fromstring(r.text)
        for article in root.findall(".//PubmedArticle"):
            rec = _parse_article(article)
            if rec:
                out[rec["pmid"]] = rec
    return out


def find_pmid(doi: str = "", title: str = "", email: str = "") -> str | None:
    """Sucht die PMID zu einer DOI oder (ersatzweise) zu einem Titel."""
    if doi:
        ids, _ = search(f'"{doi}"[DOI]', 2, email)
        if len(ids) == 1:
            return ids[0]
    if title:
        clean = re.sub(r"[^\w\s-]", " ", title)
        ids, _ = search(f"{clean}[Title]", 2, email)
        if len(ids) == 1:
            return ids[0]
    return None


def format_reference(rec: dict) -> str:
    """Vancouver-nahes Standardformat (wie in MyLuo): bis zu drei Autoren, dann "et al.".
    Wird anschließend ggf. an die Hinweise für Autoren angepasst (siehe references.py)."""
    authors = rec.get("authors") or []
    a = ", ".join(authors[:3]) + (" et al" if len(authors) > 3 else "")
    title = (rec.get("title") or "").rstrip(".")
    src = rec.get("journal", "")
    year = rec.get("year", "")
    vol = rec.get("volume", "")
    iss = f"({rec['issue']})" if rec.get("issue") else ""
    pages = f":{rec['pages']}" if rec.get("pages") else ""
    loc = f"{year};{vol}{iss}{pages}" if vol else year
    parts = [p for p in (a, title, src, loc) if p]
    ref = ". ".join(parts) + "."
    if rec.get("doi"):
        ref += f" doi:{rec['doi']}"
    elif rec.get("pmid"):
        ref += f" PMID: {rec['pmid']}"
    return ref


def is_english_or_german(lang: str) -> bool:
    return (lang or "eng").strip().lower() in ("eng", "en", "english", "ger", "de", "german", "deu")
