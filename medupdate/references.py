"""Literaturverzeichnis: Angaben über PubMed verifizieren und nach den Hinweisen für Autoren formatieren."""

from __future__ import annotations

import re

from . import prompts_new as P
from . import pubmed
from .config import MODEL_FAST
from .llm import LLM, STR, arr, obj

_PMID_RE = re.compile(r"PMID:?\s*(\d{5,9})", re.IGNORECASE)
_DOI_RE = re.compile(r"(?:doi:?\s*|https?://(?:dx\.)?doi\.org/)(10\.\d{4,9}/[^\s]+)", re.IGNORECASE)

FORMAT_SCHEMA = obj({"entries": arr(STR)})


def _numbers(t: str) -> set[str]:
    return set(re.findall(r"\d+", t))


def verify_entries(entries: list[str], known: dict[str, dict], email: str = "") -> list[str]:
    """Ersetzt Einträge mit PMID oder DOI durch die Angaben aus PubMed (korrekte Autoren, Band,
    Seiten). Einträge ohne Kennung (z.B. Leitlinien) bleiben unverändert."""
    by_doi = {r["doi"].lower(): r for r in known.values() if r.get("doi")}
    out = []
    for e in entries:
        rec = None
        m = _PMID_RE.search(e)
        d = _DOI_RE.search(e)
        doi = d.group(1).rstrip(".,;") if d else ""
        if m and m.group(1) in known:
            rec = known[m.group(1)]
        elif doi and doi.lower() in by_doi:
            rec = by_doi[doi.lower()]
        else:
            try:
                pmid = m.group(1) if m else (pubmed.find_pmid(doi=doi, email=email) if doi else None)
                if pmid:
                    rec = pubmed.fetch_records([pmid], email).get(pmid)
                    if rec:
                        known[pmid] = rec
            except Exception:
                rec = None
        # Nur übernehmen, wenn der PubMed-Datensatz plausibel zum KI-Eintrag passt (Jahr stimmt)
        if rec and (not rec.get("year") or rec["year"] in e or not re.search(r"(19|20)\d{2}", e)):
            out.append(pubmed.format_reference(rec))
        else:
            out.append(e)
    return out


def apply_style(llm: LLM, entries: list[str], rules: dict, batch: int = 30) -> list[str]:
    style = (rules or {}).get("citation_style", "").strip()
    if not style or not entries:
        return entries
    out = list(entries)
    for i in range(0, len(entries), batch):
        chunk = entries[i:i + batch]
        block = "\n".join(f"{j + 1}|||{e}" for j, e in enumerate(chunk))
        try:
            data = llm.json(P.reference_format_prompt(style, rules.get("citation_examples", ""), block),
                            FORMAT_SCHEMA, model=MODEL_FAST, max_tokens=12000, effort="low")
        except Exception:
            continue
        new = data.get("entries", [])
        if len(new) != len(chunk):
            continue
        for j, (old, cand) in enumerate(zip(chunk, new)):
            cand = re.sub(r"^\d+[.)]\s*", "", cand.split("|||")[-1]).strip()
            # Schutz: Zahlen (Jahr, Band, Seiten, PMID, DOI-Teile) dürfen sich nicht ändern
            if cand and _numbers(cand) <= _numbers(old) and len(cand) >= 0.6 * len(old):
                out[i + j] = cand
    return out


def make_formatter(llm: LLM, rules: dict, known: dict[str, dict], email: str):
    def _fmt(entries: list[str]) -> list[str]:
        return apply_style(llm, verify_entries(entries, known, email), rules)
    return _fmt
