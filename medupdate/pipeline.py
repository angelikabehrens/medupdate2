"""Ablaufsteuerung. Alle Zwischenergebnisse landen im Projekt-Dict `proj`; jeder Schritt
überspringt bereits Erledigtes. Bricht ein Durchlauf ab (z.B. Verbindung weg), setzt ein
erneuter Start genau dort fort."""

from __future__ import annotations

import base64
import gzip
import json
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from . import appraisal as A
from . import guidelines as G
from . import intake, pubmed, recommend
from . import prompts_new as P
from .citations import (
    dedupe_local_literature,
    extract_and_renumber_citations_global,
    reconcile_local_citations,
    strip_appraisal_preamble,
)
from .config import MAX_PUBS_PER_TOPIC, PARALLEL_WORKERS
from .files import extract_images_from_pdf, extract_pdf_text, fetch_oa_pdf, find_doi, looks_like_real_fulltext, \
    unpaywall, upload_matches
from .formal import run_formal_check
from .llm import LLM, STR, AccountError, arr, enum, obj
from .manuscript import build_docx, build_recommendation_docx
from .docx_tools import build_pruefprotokoll_docx
from .references import make_formatter



# Version der Kürzungsvorgabe; 2 = kürzere Sätze, weniger Statistik (2026-10-10)
SHORT_VERSION = 2

def new_project() -> dict:
    return {"version": 1, "created": date.today().isoformat(), "email": "", "toc": None, "rules": None,
            "template": None, "topics": {}, "records": {}, "pubs": {}, "uploads": {}, "style": {},
            "guideline_uploads": {}, "outputs": {}, "errors": []}


def _err(proj: dict, where: str, ex: Exception) -> None:
    proj["errors"].append({"where": where, "error": f"{type(ex).__name__}: {ex}"})
    traceback.print_exc()


class _Skipped(Exception):
    """Aufgabe wurde nach einem Konto-/Kostenlimit-Stopp nicht mehr gestartet."""


class _Streamed:
    """Ergebnis von _parallel: .items() liefert jedes Ergebnis, SOBALD es fertig ist. Die Aufrufer
    speichern es so sofort im Projekt – bricht der Lauf später ab, ist bereits Bezahltes nicht verloren."""

    def __init__(self, items, fn, log, label, workers):
        self.args = (list(items), fn, log, label, workers)

    def items(self):
        items, fn, log, label, workers = self.args
        if not items:
            return
        stop: Exception | None = None
        halt = threading.Event()

        def guarded(it):
            if halt.is_set():
                raise _Skipped()
            try:
                return fn(it)
            except AccountError:
                halt.set()  # sofort im Arbeits-Thread, damit keine weitere Anfrage mehr startet
                raise

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(guarded, it): key for key, it in items}
            for n, fut in enumerate(as_completed(futs), start=1):
                key = futs[fut]
                if fut.cancelled():
                    continue
                try:
                    res = fut.result()
                except _Skipped:
                    continue
                except Exception as e:  # einzelne Fehler sollen den Rest nicht stoppen
                    res = e
                log(f"{label}: {n}/{len(futs)} fertig")
                if isinstance(res, AccountError):
                    # Kontoproblem oder Kostenlimit: nichts Neues mehr starten, bereits laufende Aufrufe
                    # (schon bezahlt) aber noch abholen und speichern
                    stop = stop or res
                    for f in futs:
                        f.cancel()
                    continue
                yield key, res
        if stop:
            raise stop


def _parallel(items, fn, log, label: str, workers: int = PARALLEL_WORKERS) -> _Streamed:
    return _Streamed(items, fn, log, label, workers)


# ======================================================================
# SCHRITT 1
# ======================================================================

def run_intake(llm: LLM, proj: dict, toc_text: str, guide_text: str, template: tuple[str, bytes] | None,
               log) -> None:
    if not proj.get("toc"):
        log("Inhaltsverzeichnis wird ausgewertet…")
        proj["toc"] = intake.parse_toc(llm, toc_text)
        proj["topics"] = {t["id"]: {"id": t["id"], **t} for t in proj["toc"]["topics"]}
    if not proj.get("rules"):
        log("Hinweise für Autoren werden ausgewertet…")
        proj["rules"] = intake.parse_author_rules(llm, guide_text) if guide_text.strip() else {}
    if template and not proj.get("template"):
        name, data = template
        proj["template"] = {"name": name, "bytes": data, **intake.inspect_template(data)}


def run_recommendations(llm: LLM, proj: dict, log) -> None:
    chapter = (proj["toc"] or {}).get("chapter_title", "")
    rules = proj.get("rules") or {}
    n = rules.get("pubs_per_topic") or MAX_PUBS_PER_TOPIC
    todo = [(tid, t) for tid, t in proj["topics"].items() if t.get("enabled", True)
            and ("recommended" not in t or t.get("n_candidates") == 0)]

    def work(t):
        return recommend.recommend_for_topic(llm, t, chapter, rules, proj["email"], n)

    for tid, res in _parallel(todo, work, log, "Literaturempfehlung").items():
        if isinstance(res, Exception):
            _err(proj, f"Literaturempfehlung {proj['topics'][tid]['title']}", res)
            proj["topics"][tid]["rec_error"] = f"{type(res).__name__}: {res}"
            continue
        proj["topics"][tid].pop("rec_error", None)
        proj["records"].update(res.pop("records"))
        proj["topics"][tid].update(res)

    # Open-Access-Status der empfohlenen Arbeiten
    for t in proj["topics"].values():
        for pmid in t.get("recommended", []) + t.get("reserve", []):
            rec = proj["records"].get(pmid)
            if rec is not None and "is_oa" not in rec:
                info = unpaywall(rec.get("doi", ""), proj["email"])
                rec["is_oa"] = bool(info.get("is_oa"))
    fit_to_length(proj, log)


def _enabled_topics(proj: dict) -> list[str]:
    return [tid for tid, t in proj["topics"].items() if t.get("enabled", True)]


def _length_info(proj: dict, pairs: list[tuple[str, str]]) -> list[dict]:
    return [{"id": p, "length_score": proj["topics"][t].get("ranking", {}).get(p, {}).get("length_score", 50)}
            for p, t in pairs]


def _n_sections(proj: dict, topic_ids) -> int:
    return len({proj["topics"][t].get("section_number") or t for t in topic_ids}) or 1


def fit_to_length(proj: dict, log=lambda m: None) -> None:
    """Gesamtlänge laut Hinweisen für Autoren im Blick behalten: Längen vorplanen und, falls selbst
    mit ½ Seite je Arbeit das Limit überschritten wird, die schwächsten Empfehlungen in die Reserve
    verschieben (die Autorin kann sie wieder anhaken)."""
    pairs = [(p, tid) for tid in _enabled_topics(proj) for p in proj["topics"][tid].get("recommended", [])]
    tiers, overflow = A.plan_tiers(_length_info(proj, pairs), proj.get("rules") or {},
                                   _n_sections(proj, {t for _, t in pairs}))
    for p, tid in pairs:
        if p in overflow:
            t = proj["topics"][tid]
            t["recommended"].remove(p)
            t.setdefault("reserve", []).insert(0, p)
            if p in t.get("selected", []):
                t["selected"].remove(p)
    if overflow:
        log(f"{len(overflow)} Empfehlung(en) wegen der vorgegebenen Gesamtlänge in die Reserve verschoben.")
    proj["length_plan"] = {"tiers": tiers, "overflow": overflow,
                           "pages": A.estimate_pages(tiers, _n_sections(proj, {t for _, t in pairs})),
                           "max_pages": (proj.get("rules") or {}).get("max_pages") or 0}


def run_guideline_search(llm: LLM, proj: dict, log) -> None:
    chapter = (proj["toc"] or {}).get("chapter_title", "")
    todo = [(tid, t) for tid, t in proj["topics"].items() if t.get("enabled", True) and "guidelines" not in t]

    def work(t):
        found = G.search_guidelines(llm, t["title"], chapter)
        for g in found:
            text, src = G.fetch_guideline_text(g)
            g["text"], g["source"] = text, src
            g["status"] = "geladen" if text else "fehlt"
        return found

    for tid, res in _parallel(todo, work, log, "Leitlinienrecherche").items():
        if isinstance(res, Exception):
            _err(proj, f"Leitlinien {proj['topics'][tid]['title']}", res)
            proj["topics"][tid]["guidelines"] = []
        else:
            proj["topics"][tid]["guidelines"] = res


def recommendation_docx(proj: dict) -> bytes:
    rankings = {}
    for t in proj["topics"].values():
        rankings.update(t.get("ranking", {}))
    topics = [t for t in proj["topics"].values() if t.get("enabled", True)]
    return build_recommendation_docx((proj["toc"] or {}).get("chapter_title", ""), topics, proj["records"], rankings)


# ======================================================================
# SCHRITT 2: Uploads zuordnen
# ======================================================================

IDENT_SCHEMA = obj({"title": STR, "authors": arr(STR), "journal": STR, "year": STR, "doi": STR,
                    "kind": enum("original", "review", "guideline", "other")})
ASSIGN_SCHEMA = obj({"assignments": arr(obj({"key": STR, "topic_id": STR}))})


def selected_pmids(proj: dict) -> list[str]:
    out = []
    for t in proj["topics"].values():
        if t.get("enabled", True):
            out.extend(t.get("selected", t.get("recommended", [])))
    return out


def match_uploads(llm: LLM, proj: dict, uploads: dict[str, bytes], log) -> None:
    """Ordnet hochgeladene PDFs den empfohlenen Publikationen zu. Nicht zuordenbare Dateien werden
    identifiziert (DOI → PubMed, sonst KI) und als zusätzliche Publikationen einem Thema zugeordnet."""
    wanted = {p: proj["records"][p] for p in selected_pmids(proj) if p in proj["records"]}
    unmatched = []
    for name, data in uploads.items():
        if name in proj["uploads"]:
            # Bereits zugeordnet (z.B. aus einem geladenen Zwischenstand ohne PDFs): PDF nur für die
            # Abbildungsauswahl wieder anhängen, Text und Zuordnung bleiben
            pm = proj["uploads"][name].get("pmid")
            d = proj["pubs"].get(pm or "", {})
            if pm and "pdf" not in d and "images" not in d:
                d["pdf"] = data
            continue
        text = extract_pdf_text(data)
        hit = next((p for p, rec in wanted.items() if upload_matches(text, rec)), None)
        proj["uploads"][name] = {"pmid": hit, "text_len": len(text)}
        if hit:
            proj["pubs"].setdefault(hit, {})["pdf"] = data
            proj["pubs"][hit]["full_text"] = text
        else:
            unmatched.append((name, data, text))

    extra = []
    for name, data, text in unmatched:
        log(f"Zusätzliche Datei „{name}“ wird identifiziert…")
        doi = find_doi(text)
        pmid = pubmed.find_pmid(doi=doi, email=proj["email"]) if doi else None
        if not pmid:
            try:
                meta = llm.json(P.identify_pub_prompt(text[:5000]), IDENT_SCHEMA, max_tokens=2000, effort="low")
            except Exception as ex:
                _err(proj, f"Identifikation {name}", ex)
                continue
            if meta.get("kind") == "guideline":
                proj["uploads"][name]["role"] = "background"
                proj["guideline_uploads"][name] = {"topic_id": "", "text": text}
                continue
            pmid = pubmed.find_pmid(doi=meta.get("doi", ""), title=meta.get("title", ""), email=proj["email"])
            if not pmid:
                pmid = f"upload:{name}"
                proj["records"][pmid] = {"pmid": "", "title": meta["title"], "authors": meta["authors"],
                                         "journal": meta["journal"], "year": meta["year"], "doi": meta["doi"],
                                         "abstract": "", "pub_types": [], "volume": "", "issue": "", "pages": ""}
        if pmid not in proj["records"]:
            proj["records"].update(pubmed.fetch_records([pmid], proj["email"]))
        proj["uploads"][name]["pmid"] = pmid
        proj["pubs"].setdefault(pmid, {}).update({"pdf": data, "full_text": text, "extra": True})
        extra.append(pmid)

    if extra:
        topics_block = "\n".join(f"{t['id']}: {t['title']}" for t in proj["topics"].values() if t.get("enabled", True))
        pubs_block = "\n".join(f"{p}: {proj['records'].get(p, {}).get('title', '')}" for p in extra)
        try:
            data = llm.json(P.topic_assign_prompt(topics_block, pubs_block), ASSIGN_SCHEMA, max_tokens=3000,
                            effort="low")
            for a in data.get("assignments", []):
                if a["key"] in proj["pubs"] and a["topic_id"] in proj["topics"]:
                    proj["pubs"][a["key"]]["topic_id"] = a["topic_id"]
        except Exception as ex:
            _err(proj, "Themenzuordnung", ex)


# ======================================================================
# SCHRITT 2: Automatische Erstellung
# ======================================================================

def _pub_list(proj: dict) -> list[tuple[str, str]]:
    """[(pmid, topic_id)] in Manuskript-Reihenfolge: Themen wie im Inhaltsverzeichnis, darin nach Score."""
    out = []
    for tid, t in proj["topics"].items():
        if not t.get("enabled", True):
            continue
        ids = list(t.get("selected", t.get("recommended", [])))
        ids += [p for p, d in proj["pubs"].items() if d.get("extra") and d.get("topic_id") == tid and p not in ids]
        rk = t.get("ranking", {})
        ids.sort(key=lambda p: -rk.get(p, {}).get("score", 0))
        out.extend((p, tid) for p in ids)
    return out


def _guideline_docs(proj: dict, tid: str, query: str, per_doc: int = 6000, total: int = 14000) -> list:
    docs = []
    t = proj["topics"][tid]
    for g in t.get("guidelines", []):
        if g.get("text"):
            label = f"Leitlinie: {g.get('title', '')} ({g.get('society', '')}, {g.get('year', '')})"
            docs.append((label, G.relevant_excerpt(g["text"], query, per_doc)))
    for name, g in proj.get("guideline_uploads", {}).items():
        if g.get("topic_id") in ("", tid) and g.get("text"):
            docs.append((f"Vom Autor bereitgestellt: {name}", G.relevant_excerpt(g["text"], query, per_doc)))
    out, used = [], 0
    for name, text in docs:
        if used + len(text) > total:
            break
        out.append((name, text))
        used += len(text)
    return out


def _entry(rec: dict, pub: dict) -> dict:
    return {"title": rec.get("title", ""), "journal": rec.get("journal", ""), "pubdate": rec.get("year", ""),
            "pub_types": rec.get("pub_types") or ["(unbekannt)"], "abstract": rec.get("abstract") or "(kein Abstract)",
            "full_text": _main_text(pub.get("full_text") or "")[:35000]}


def _main_text(text: str) -> str:
    """Volltext ohne Literaturverzeichnis und Anhang (spart Token, enthält keine Ergebnisse)."""
    m = re.search(r"\n\s*(References|REFERENCES|Literatur|Bibliography)\s*\n", text[len(text) // 3:])
    return text[: len(text) // 3 + m.start()] if m else text


FIG_SCHEMA = obj({"index": {"type": "integer"}, "typ": enum("Tabelle", "Abbildung"), "caption_en": STR,
                  "caption_de": STR})


def _pick_figure(llm: LLM, title: str, images: list[bytes]) -> dict | None:
    from PIL import Image  # Pillow wird von Streamlit mitinstalliert
    import io
    content, kept = [], []
    for img in images[:12]:
        try:
            im = Image.open(io.BytesIO(img))
            if im.width < 250 or im.height < 150:
                continue
            im = im.convert("RGB")
            im.thumbnail((1400, 1400))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            data = buf.getvalue()
        except Exception:
            continue
        kept.append(data)
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(data).decode()}})
    if not kept:
        return None
    content.append({"type": "text", "text": P.figure_pick_prompt(title, len(kept))})
    res = llm.json(content, FIG_SCHEMA, max_tokens=1500, effort="low")
    i = res.get("index", -1)
    if not isinstance(i, int) or not (0 <= i < len(kept)):
        return None
    return {"bytes": kept[i], "typ": res["typ"], "caption_en": res["caption_en"], "caption_de": res["caption_de"]}


def run_manuscript(llm: LLM, proj: dict, log, options: dict) -> None:
    email = proj["email"]
    pubs = _pub_list(proj)
    if options.get("trial"):
        # Probelauf: erstes Thema mit Publikationen, höchstens 2 Arbeiten – kostet nur einen Bruchteil
        first = next((t for _, t in pubs), None)
        pubs = [(p, t) for p, t in pubs if t == first][:2]
    log(f"{len(pubs)} Publikationen in {len({t for _, t in pubs})} Themen werden verarbeitet.")

    # 1. Volltexte (Upload oder Open Access) und Abbildungen
    for pmid, _tid in pubs:
        d = proj["pubs"].setdefault(pmid, {})
        rec = proj["records"].get(pmid, {})
        if not d.get("full_text") and not d.get("oa_tried"):
            d["oa_tried"] = True
            pdf = fetch_oa_pdf(rec.get("doi", ""), email)
            if pdf:
                text = extract_pdf_text(pdf)
                if looks_like_real_fulltext(text, rec.get("abstract", "")):
                    d["pdf"], d["full_text"], d["fulltext_source"] = pdf, text, "Open Access"
        if d.get("pdf") and "images" not in d and options.get("figures", True):
            d["images"] = extract_images_from_pdf(d["pdf"], max_images=8)
        if d.get("full_text") and ("images" in d or not options.get("figures", True)):
            d.pop("pdf", None)  # Text und Bildkandidaten sind extrahiert: PDF nicht mehr im Speicher halten

    # 2. Kritische Würdigung (englisch, wie in MyLuo als Arbeitsfassung)
    todo = [(p, (p, t)) for p, t in pubs if not proj["pubs"][p].get("appraisal_en")]

    def appraise(item):
        p, tid = item
        rec = proj["records"].get(p, {})
        query = f"{proj['topics'][tid]['title']} {rec.get('title', '')} {rec.get('abstract', '')[:600]}"
        return A.generate_appraisal(llm, _entry(rec, proj["pubs"][p]), _guideline_docs(proj, tid, query), "Englisch")

    log("Kritische Würdigungen werden erstellt (mit Websuche, das dauert mehrere Minuten pro Publikation)…")
    for p, res in _parallel(todo, appraise, log, "Kritische Würdigung").items():
        if isinstance(res, Exception):
            _err(proj, f"Würdigung {proj['records'].get(p, {}).get('title', p)[:60]}", res)
        else:
            proj["pubs"][p]["appraisal_en"] = res

    ready = [(p, t) for p, t in pubs if proj["pubs"][p].get("appraisal_en")]

    # 3. Lokale Literaturliste (Nr. 1 = Publikation selbst) und Längenstufen
    for p, _ in ready:
        d = proj["pubs"][p]
        if "body_en" not in d:
            body, entries = extract_and_renumber_citations_global(d["appraisal_en"], 2)
            main = pubmed.format_reference(proj["records"].get(p, {}))
            body, lit = dedupe_local_literature(body, [main] + entries)
            d["body_en"], d["literature"] = strip_appraisal_preamble(body), lit
    if not proj.get("tiers") or options.get("replan_tiers") or any(p not in proj["tiers"] for p, _ in ready):
        # Selbst ausgewählte Arbeiten werden nicht verworfen: im Notfall alle auf ½ Seite, Hinweis im Protokoll
        tiers, overflow = A.plan_tiers(_length_info(proj, ready), proj.get("rules") or {},
                                       _n_sections(proj, {t for _, t in ready}))
        for p in overflow:
            tiers[p] = "½ Seite (Kurz)"
        proj["tiers"] = tiers
    for p, _ in ready:
        proj["tiers"].setdefault(p, "½ Seite (Kurz)")

    # 4. Kürzung (englisch). Auf Wunsch werden ältere Kürzungen (vor SHORT_VERSION) neu erstellt; dann
    # auch Übersetzung und Stilfassung, die darauf aufbauen.
    if options.get("reshorten"):
        for p, _ in ready:
            d = proj["pubs"][p]
            if d.get("short_en") and d.get("short_v", 1) < SHORT_VERSION:
                for k in ("short_en", "short_de", "styled_de", "style_ok"):
                    d.pop(k, None)
    todo = [(p, p) for p, _ in ready if not proj["pubs"][p].get("short_en")]

    def shorten(p):
        d, rec = proj["pubs"][p], proj["records"].get(p, {})
        return A.shorten(llm, d["body_en"], proj["tiers"][p], "Englisch", rec.get("abstract", ""))

    log("Würdigungen werden auf die geplanten Längen gekürzt…")
    for p, res in _parallel(todo, shorten, log, "Kürzung").items():
        if isinstance(res, Exception):
            _err(proj, f"Kürzung {p}", res)
            proj["pubs"][p]["short_en"] = proj["pubs"][p]["body_en"]  # ungekürzt weiterverwenden
        else:
            proj["pubs"][p]["short_en"], proj["pubs"][p]["short_v"] = res, SHORT_VERSION

    # 5. Übersetzung ins Deutsche
    todo = [(p, p) for p, _ in ready if not proj["pubs"][p].get("short_de")]

    def to_de(p):
        d, rec = proj["pubs"][p], proj["records"].get(p, {})
        out = {"short_de": A.translate_structured(llm, d["short_en"])}
        if proj["tiers"][p] == "2 Seiten" and rec.get("abstract"):
            out["abstract_de"] = A.translate_abstract(llm, rec["abstract"])
        return out

    log("Übersetzung ins Deutsche…")
    for p, res in _parallel(todo, to_de, log, "Übersetzung").items():
        if isinstance(res, Exception):
            _err(proj, f"Übersetzung {p}", res)
        else:
            proj["pubs"][p].update(res)

    # 6. State of the Art: einer je Hauptabschnitt, vor dem Thema mit den meisten Publikationen
    topic_ids = [t for t in proj["topics"] if any(tt == t for _, tt in ready)]
    counts = {t: sum(1 for _, tt in ready if tt == t) for t in topic_ids}
    sota_ids = A.sota_topics(topic_ids, proj["topics"], counts)
    proj["sota_topics"] = sota_ids

    def first_pub(tid):
        """Die erste Publikation des Themas mit Volltext (sie folgt direkt auf den State of the Art)."""
        return next((p for p, tt in ready if tt == tid and proj["pubs"][p].get("full_text")), None)

    # Neu erstellen, wenn noch keiner da ist oder ein älterer (per Websuche) durch einen aus der
    # Einleitung der folgenden Publikation ersetzt werden kann
    todo = [(t, t) for t in sota_ids if not proj["topics"][t].get("sota_en")
            or (proj["topics"][t]["sota_en"].get("source") != "intro" and first_pub(t))]

    def sota(tid):
        title = proj["topics"][tid]["title"]
        en = None
        p = first_pub(tid)
        if p:
            en = A.generate_sota_from_intro(llm, title, proj["records"].get(p, {}).get("title", ""),
                                            proj["pubs"][p]["full_text"])
        if en is None:
            en = A.generate_sota(llm, title, "Englisch", _guideline_docs(proj, tid, title, per_doc=5000, total=12000))
        return {"sota_en": en, "sota_de": A.translate_sota(llm, en)}

    if options.get("sota", True):
        log(f"State of the Art wird erstellt ({len(sota_ids)} Hauptabschnitt(e))…")
        for t, res in _parallel(todo, sota, log, "State of the Art").items():
            if isinstance(res, Exception):
                _err(proj, f"State of the Art {proj['topics'][t]['title']}", res)
            else:
                proj["topics"][t].update(res)

    # 7. Stilanpassung (deutsch) an frühere Manuskripte
    samples = list(proj.get("style", {}).values())
    if samples and options.get("style", True):
        if not proj.get("style_profile"):
            log("Schreibstil der früheren Manuskripte wird analysiert…")
            proj["style_profile"] = A.style_profile(llm, samples)
        todo = [(p, p) for p, _ in ready if proj["pubs"][p].get("short_de") and "styled_de" not in proj["pubs"][p]]
        excerpt = samples[0][1000:3500]

        def styl(p):
            return A.apply_style(llm, proj["pubs"][p]["short_de"], proj["style_profile"], excerpt)

        log("Stilanpassung…")
        for p, res in _parallel(todo, styl, log, "Stilanpassung").items():
            if isinstance(res, Exception):
                _err(proj, f"Stilanpassung {p}", res)
            else:
                proj["pubs"][p]["styled_de"], proj["pubs"][p]["style_ok"] = res

    # 8. Abbildungen
    if options.get("figures", True):
        max_fig = (proj.get("rules") or {}).get("max_figures") or 15
        cand = [p for p, _ in ready if proj["pubs"][p].get("images") and "figure" not in proj["pubs"][p]
                and proj["tiers"].get(p) not in ("Kurznachricht", "½ Seite (Kurz)")]
        already = sum(1 for p, _ in ready if proj["pubs"][p].get("figure"))
        cand = cand[: max(0, max_fig - already)]

        def fig(p):
            return _pick_figure(llm, proj["records"].get(p, {}).get("title", ""), proj["pubs"][p]["images"])

        log("Abbildungen werden ausgewählt…")
        for p, res in _parallel([(p, p) for p in cand], fig, log, "Abbildungen").items():
            proj["pubs"][p]["figure"] = None if isinstance(res, Exception) else res
    for p, _ in ready:  # Speicher freigeben: Kandidatenbilder werden nicht mehr gebraucht
        if "figure" in proj["pubs"][p]:
            proj["pubs"][p].pop("images", None)

    # 9. Manuskripte zusammensetzen, Literatur zusammenführen, Formatvorlage, formale Prüfung
    log("Gesamtmanuskript wird zusammengesetzt und formal geprüft…")
    rules = proj.get("rules") or {}
    title = (proj["toc"] or {}).get("chapter_title") or "Gesamtmanuskript"
    template = (proj.get("template") or {}).get("bytes")
    formatter = make_formatter(llm, rules, proj["records"], email)
    gliederung = outline(proj, topic_ids)
    for lang in ("Deutsch", "Englisch"):
        blocks = []
        for tid in topic_ids:
            t = proj["topics"][tid]
            sota_block = (t.get("sota_de" if lang == "Deutsch" else "sota_en")
                          if tid in sota_ids and options.get("sota", True) else None)
            block = {"topic": t["title"], "sota": sota_block, "pubs": [], **gliederung.get(tid, {})}
            for p, tt in ready:
                if tt != tid:
                    continue
                d, rec = proj["pubs"][p], proj["records"].get(p, {})
                if lang == "Deutsch":
                    text = d.get("styled_de") or d.get("short_de") or d["short_en"]
                    abstract = d.get("abstract_de")
                else:
                    text = d["short_en"]
                    abstract = rec.get("abstract") if proj["tiers"].get(p) == "2 Seiten" else None
                text, lit = reconcile_local_citations(text, d["literature"])
                fig = d.get("figure")
                figure = ({"bytes": fig["bytes"], "typ": fig["typ"],
                           "caption": fig["caption_de" if lang == "Deutsch" else "caption_en"]} if fig else None)
                block["pubs"].append({"title": rec.get("title", p), "rec": rec, "abstract": abstract,
                                      "lines": [l for l in text.split("\n") if l.strip()], "literature": lit,
                                      "figure": figure})
            blocks.append(block)
        raw = build_docx(title, blocks, lang)
        key = "DE" if lang == "Deutsch" else "EN"
        try:
            befunde, final = run_formal_check(raw, llm if lang == "Deutsch" else None, template, rules, formatter)
        except Exception as ex:
            _err(proj, f"Formale Prüfung {key}", ex)
            befunde, final = [], raw
        proj["outputs"][f"manuskript_{key}"] = final
        proj["outputs"][f"rohfassung_{key}"] = raw
        if lang == "Deutsch":
            befunde = _pipeline_findings(proj, ready) + befunde
            proj["outputs"]["befunde"] = befunde
            proj["outputs"]["pruefprotokoll"] = build_pruefprotokoll_docx(befunde)
    log("Fertig.")


def _pipeline_findings(proj: dict, ready: list) -> list[dict]:
    """Zusätzliche Hinweise für das Prüfprotokoll: fehlende Volltexte, Leitlinien, Stilanpassung, Fehler."""
    out = []
    no_ft = [proj["records"].get(p, {}).get("title", p)[:70] for p, _ in ready if not proj["pubs"][p].get("full_text")]
    out.append({"punkt": "Volltexte", "status": "✅" if not no_ft else "⚠️",
                "begruendung": "Alle Würdigungen beruhen auf dem Volltext." if not no_ft else
                "Nur auf Basis des Abstracts gewürdigt: " + "; ".join(no_ft)})
    missing = []
    for t in proj["topics"].values():
        for g in t.get("guidelines", []):
            if not g.get("text") and g.get("status") != "ersetzt":
                missing.append(f"{g.get('title', '')} ({g.get('society', '')})")
    out.append({"punkt": "Leitlinien", "status": "✅" if not missing else "⚠️",
                "begruendung": "Alle gefundenen Leitlinien lagen im Volltext vor." if not missing else
                "Nicht im Volltext verfügbar (nur über die Websuche berücksichtigt): " + "; ".join(missing[:10])})
    max_pages = (proj.get("rules") or {}).get("max_pages") or 0
    tiers = {p: proj["tiers"][p] for p, _ in ready if p in proj.get("tiers", {})}
    est = A.estimate_pages(tiers, _n_sections(proj, {t for _, t in ready}))
    counts = {}
    for t in tiers.values():
        counts[t] = counts.get(t, 0) + 1
    over = bool(max_pages and est > max_pages)
    out.append({"punkt": "Längenplanung (nach Studienqualität, Bedeutung der Fragestellung, Impact Factor)",
                "status": "⚠️" if over else "✅",
                "begruendung": ", ".join(f"{n}× {t}" for t, n in counts.items()) + f"; geschätzt ≈{est} Seiten"
                + (f" bei max. {max_pages} Seiten – Limit überschritten, bitte Auswahl reduzieren." if over
                   else (f" bei max. {max_pages} Seiten." if max_pages else "."))})
    styled = [p for p, _ in ready if "style_ok" in proj["pubs"][p]]
    if styled:
        bad = [p for p in styled if not proj["pubs"][p]["style_ok"]]
        out.append({"punkt": "Stilanpassung", "status": "✅" if not bad else "⚠️",
                    "begruendung": f"{len(styled) - len(bad)} von {len(styled)} Publikationen angepasst." +
                    (" Bei den übrigen wurde die Anpassung verworfen, weil sich Zahlen oder Umfang verändert "
                     "hätten." if bad else "")})
    if proj["errors"]:
        out.append({"punkt": "Fehler während der Erstellung", "status": "⚠️",
                    "begruendung": "; ".join(f"{e['where']}: {e['error'][:120]}" for e in proj["errors"][-10:])})
    return out


# ======================================================================
# Zwischenstand speichern / laden (die App speichert nichts auf dem Server)
# ======================================================================

def _enc(o):
    if isinstance(o, bytes):
        return {"__b64__": base64.b64encode(o).decode()}
    if isinstance(o, set):
        return list(o)
    raise TypeError(type(o))


def _dec(d):
    if isinstance(d, dict) and set(d) == {"__b64__"}:
        return base64.b64decode(d["__b64__"])
    return d


def export_project(proj: dict) -> bytes:
    """Zwischenstand ohne PDFs und Kandidatenbilder (die Texte sind enthalten) – hält die Datei klein."""
    slim = dict(proj)
    slim["pubs"] = {p: {k: v for k, v in d.items() if k not in ("pdf", "images")} for p, d in proj["pubs"].items()}
    return gzip.compress(json.dumps(slim, default=_enc, ensure_ascii=False).encode("utf-8"), 6)


def outline(proj: dict, topic_ids: list[str]) -> dict[str, dict]:
    """Gliederung nach dem hochgeladenen Inhaltsverzeichnis, aber nur mit Themen, zu denen Publikationen
    im Manuskript stehen; Hauptabschnitte, Unterabschnitte und Themen werden fortlaufend neu nummeriert.
    Rückgabe je Thema: {"number": "8.1.2.1" | None, "section": ("8.1", Titel) | None,
    "subsection": ("8.1.2", Titel) | None}. "section"/"subsection" stehen nur beim ersten Thema eines
    neuen (Unter-)Abschnitts. Ein (Unter-)Abschnitt mit nur einem gleichnamigen Thema erscheint nur einmal."""
    chapter = str((proj.get("toc") or {}).get("chapter_number") or "").strip().rstrip(".")
    prefix = f"{chapter}." if chapter else ""
    # Hauptabschnitt -> Einträge (Unterabschnitt mit Themen oder Thema direkt), in TOC-Reihenfolge
    sections: dict[str, dict[str, list[str]]] = {}
    titles: dict[str, str] = {}
    sub_titles: dict[tuple[str, str], str] = {}
    for tid in proj["topics"]:
        if tid not in topic_ids:
            continue
        t = proj["topics"][tid]
        key = t.get("section_number") or t.get("section_title") or tid
        sub = t.get("subsection_number") or t.get("subsection_title") or ""
        child = f"sub:{sub}" if sub else f"topic:{tid}"
        sections.setdefault(key, {}).setdefault(child, []).append(tid)
        titles.setdefault(key, t.get("section_title") or t["title"])
        if sub:
            sub_titles.setdefault((key, sub), t.get("subsection_title") or sub)

    def same(a: str, b: str) -> bool:
        return a.strip().lower() == b.strip().lower()

    out = {}
    for s_idx, (key, children) in enumerate(sections.items(), start=1):
        sec_num = f"{prefix}{s_idx}"
        all_tids = [x for tids in children.values() for x in tids]
        if len(all_tids) == 1 and same(titles[key], proj["topics"][all_tids[0]]["title"]):
            out[all_tids[0]] = {"section": (sec_num, titles[key]), "subsection": None, "number": None, "level": 2}
            continue
        first = True
        for c_idx, (child, tids) in enumerate(children.items(), start=1):
            num = f"{sec_num}.{c_idx}"
            sec = (sec_num, titles[key]) if first else None
            first = False
            if child.startswith("topic:"):
                out[tids[0]] = {"section": sec, "subsection": None, "number": num, "level": 2}
                continue
            st = sub_titles[(key, child[4:])]
            if len(tids) == 1 and same(st, proj["topics"][tids[0]]["title"]):
                out[tids[0]] = {"section": sec, "subsection": None, "number": num, "level": 2}
                continue
            for t_idx, tid in enumerate(tids, start=1):
                out[tid] = {"section": sec if t_idx == 1 else None,
                            "subsection": (num, st) if t_idx == 1 else None,
                            "number": f"{num}.{t_idx}", "level": 3}
    return out


def partial_docx(proj: dict) -> bytes | None:
    """Alle bisher fertigen Würdigungen als Word-Datei (beste verfügbare Fassung), auch nach einem Abbruch."""
    blocks = []
    for tid, t in proj["topics"].items():
        pubs = []
        for p, d in proj["pubs"].items():
            if p in t.get("selected", t.get("recommended", [])) or (d.get("extra") and d.get("topic_id") == tid):
                text = d.get("styled_de") or d.get("short_de") or d.get("short_en") or d.get("body_en")
                if text:
                    rec = proj["records"].get(p, {})
                    pubs.append({"title": rec.get("title", p), "rec": rec, "abstract": None,
                                 "lines": [x for x in text.split("\n") if x.strip()],
                                 "literature": d.get("literature", []), "figure": None})
        if pubs:
            blocks.append({"topic": t["title"], "sota": t.get("sota_de"), "pubs": pubs})
    if not blocks:
        return None
    return build_docx("Bisherige Ergebnisse (unvollständig)", blocks, "Deutsch")


def import_project(data: bytes) -> dict:
    """Lädt einen Zwischenstand (komprimiert .json.gz oder ältere unkomprimierte .json)."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    proj = json.loads(data.decode("utf-8"), object_hook=_dec)
    for d in (proj.get("pubs") or {}).values():  # ältere Zwischenstände: große Binärdaten verwerfen
        d.pop("images", None)
    base = new_project()
    base.update(proj)
    return base
