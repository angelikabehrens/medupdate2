"""Literaturverzeichnis: Angaben über PubMed verifizieren und nach den Hinweisen für Autoren formatieren."""

from __future__ import annotations

import hashlib
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


REFS_CACHE_MAX = 3000


def apply_style(llm: LLM, entries: list[str], rules: dict, batch: int = 30, cache: dict | None = None) -> list[str]:
    """cache (optional, proj["build_cache"]["refs"]): bereits formatierte Einträge werden übernommen; an die KI
    gehen nur neue. Das spart beim erneuten Erstellen und schon beim ersten Mal die zweite Formatierung derselben
    Liste für die englische Fassung."""
    style = (rules or {}).get("citation_style", "").strip()
    if not style or not entries:
        return entries
    examples = rules.get("citation_examples", "")
    out = list(entries)

    def key(entry: str) -> str:
        return hashlib.md5(f"{style}|{examples}|{entry}".encode("utf-8")).hexdigest()[:20]

    todo = list(range(len(entries)))
    if cache is not None:
        todo = []
        for i, e in enumerate(entries):
            k = key(e)
            if k in cache:
                out[i] = cache[k]
            else:
                todo.append(i)
    for n in range(0, len(todo), batch):
        idx = todo[n:n + batch]
        chunk = [entries[i] for i in idx]
        block = "\n".join(f"{j + 1}|||{e}" for j, e in enumerate(chunk))
        try:
            data = llm.json(P.reference_format_prompt(style, examples, block),
                            FORMAT_SCHEMA, model=MODEL_FAST, max_tokens=12000, effort="low")
        except Exception:
            continue
        new = data.get("entries", [])
        if len(new) != len(chunk):
            continue
        for i, old, cand in zip(idx, chunk, new):
            cand = re.sub(r"^\d+[.)]\s*", "", cand.split("|||")[-1]).strip()
            # Schutz: Zahlen (Jahr, Band, Seiten, PMID, DOI-Teile) dürfen sich nicht ändern
            if cand and _numbers(cand) <= _numbers(old) and len(cand) >= 0.6 * len(old):
                out[i] = cand
                if cache is not None:
                    cache[key(old)] = cand
                    while len(cache) > REFS_CACHE_MAX:
                        cache.pop(next(iter(cache)))
    return out


def make_formatter(llm: LLM, rules: dict, known: dict[str, dict], email: str, cache: dict | None = None):
    def _fmt(entries: list[str]) -> list[str]:
        return apply_style(llm, verify_entries(entries, known, email), rules, cache=cache)
    return _fmt
