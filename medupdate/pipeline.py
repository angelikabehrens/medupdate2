"""Ablaufsteuerung. Alle Zwischenergebnisse landen im Projekt-Dict `proj`; jeder Schritt
überspringt bereits Erledigtes. Bricht ein Durchlauf ab (z.B. Verbindung weg), setzt ein
erneuter Start genau dort fort."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import random
import re
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

from . import appraisal as A
from . import guidelines as G
from . import intake, prior, pubmed, recommend
from . import prompts_new as P
from .citations import (
    dedupe_local_literature,
    extract_and_renumber_citations_global,
    reconcile_local_citations,
    strip_appraisal_preamble,
)
from .config import APP_VERSION, MAX_CANDIDATES_PER_TOPIC, MAX_PUBS_PER_TOPIC, MODEL_FAST, MODEL_MAIN, PARALLEL_WORKERS
from .files import extract_pdf_text, fetch_oa_pdf, find_doi, looks_like_real_fulltext, \
    unpaywall, upload_matches
from .formal import run_formal_check
from .llm import BOOL, LLM, STEP_APPRAISAL, STEP_APPRAISAL_LEAN, STR, AccountError, arr, enum, obj, \
    run_job
from .manuscript import build_docx, build_recommendation_docx
from .docx_tools import build_pruefprotokoll_docx
from .references import make_formatter



# Version der Kürzungsvorgabe; 2 = kürzere Sätze, weniger Statistik (2026-10-10); 3 = Methodik verständlich
# erklärt, abwechslungsreiches Fazit (2026-10-11); 9 = kurzes Fazit: Qualität ohne Begründung + Praxisaussage (2026-10-14);
# 10 = Sprachstil nach dem Vorbild Decker: Hinleitung aus der Einleitung, Konnektoren, Kommentar in vier Bausteinen,
# Kurznachricht mit "(1)" hinter der Studie (2026-10-15); 11 = neue Längen: lang höchstens 1 Seite (370 statt 420
# Wörter), mittel ½ Seite, kurz ⅓ Seite (2026-11-03)
SHORT_VERSION = 11
MAX_GROUP_SIZE = 5  # gemeinsame Würdigung von bis zu 5 Publikationen (Angelika 2026-10-13)
GROUP_VERSION = 3  # höher setzen, wenn sich die Regeln für gemeinsame Würdigungen ändern

def new_project() -> dict:
    return {"version": 1, "id": uuid.uuid4().hex[:12], "created": date.today().isoformat(), "email": "", "toc": None, "rules": None,
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


# Toleranz für die manuell eingegebenen Obergrenzen (Seiten, Literaturstellen)
LIMIT_TOLERANCE = {"mode": "absolute", "value": 10}


def effective_limit(value: int, tol: dict | None = None) -> int:
    tol = tol or LIMIT_TOLERANCE
    if not value:
        return 0
    if tol.get("mode") == "absolute":
        return int(value + tol.get("value", 0))
    return int(value * (1 + tol.get("value", 0) / 100.0))


def apply_manual_limits(proj: dict) -> None:
    """Manuell eingegebene Vorgaben (Seiten ohne Inhaltsverzeichnis, Literaturstellen) haben Vorrang vor
    den Hinweisen für Autoren. Die Grenze ist der eingegebene Wert plus Toleranz; für die Seitenplanung kommt
    die Titelseite mit dem Inhaltsverzeichnis hinzu."""
    lim = proj.get("manual_limits") or {}
    if proj.get("rules") is None:
        proj["rules"] = {}
    rules = proj["rules"]
    tol = proj.get("limit_tolerance") or LIMIT_TOLERANCE
    guide = rules.setdefault("_guide_limits", {"max_pages": rules.get("max_pages") or 0,
                                               "max_references": rules.get("max_references") or 0})
    rules["max_pages"] = effective_limit(lim["pages"], tol) + 1 if lim.get("pages") else guide["max_pages"]
    rules["max_references"] = (effective_limit(lim["references"], tol) if lim.get("references")
                               else guide["max_references"])
    # Höchstzahl gewürdigter Publikationen: genau der eingegebene Wert (keine Toleranz)
    rules["max_publications"] = int(lim.get("publications") or 0)
    # Feste Zahl je Würdigungslänge (lang / mittel / kurz / Kurznachricht)
    rules["tier_counts"] = dict(proj.get("tier_counts") or A.DEFAULT_TIER_COUNTS)


NO_TOLERANCE = {"mode": "absolute", "value": 0}


def set_strict_limits(proj: dict, strict: bool) -> None:
    """Häkchen „Keine Toleranz“ (Angelika 2026-10-14): Seiten und Literaturstellen sind dann harte
    Obergrenzen wie die Publikationszahl."""
    proj["limit_tolerance"] = dict(NO_TOLERANCE) if strict else dict(LIMIT_TOLERANCE)
    apply_manual_limits(proj)


def tolerance_note(proj: dict) -> str:
    tol = proj.get("limit_tolerance") or LIMIT_TOLERANCE
    return ("keine Überschreitung erlaubt" if not tol.get("value")
            else f"Überschreitung um höchstens {tolerance_text(proj)}")


def tolerance_text(proj: dict) -> str:
    tol = proj.get("limit_tolerance") or LIMIT_TOLERANCE
    if not tol.get("value"):
        return "0 (keine Toleranz)"
    if tol.get("mode") != "absolute":
        return f"{tol['value']} %"
    return f"{tol['value']} Seiten bzw. {tol['value']} Literaturstellen"


SAFETY = "NEU"  # Sicherheitsnetz: wichtige Arbeiten, die zu keinem Thema des Inhaltsverzeichnisses passen


def _search_topics(proj: dict) -> list[tuple[str, dict]]:
    return [(tid, t) for tid, t in proj["topics"].items()
            if t.get("enabled", True) and not t.get("varia") and not t.get("safety")]


def run_search_plan(llm: LLM, proj: dict, log, force: bool = False) -> None:
    """Schritt 1a (günstig): Kapitel analysieren (Gliederung, Anker, englische Titel, nicht suchbare
    Überschriften), je Thema den Suchplan erstellen und die Trefferzahlen in PubMed zählen. Bewertet wird
    noch nichts; die Autorin kann die Suchen vorher prüfen und ändern."""
    chapter = (proj["toc"] or {}).get("chapter_title", "")
    rules = proj.get("rules") or {}
    von, bis = recommend.search_window(rules, proj.get("search_window"))
    topics = _search_topics(proj)
    if force or not proj.get("search_frame"):
        log("Kapitel wird analysiert (Gliederung, Suchanker)…")
        proj["search_frame"] = recommend.analyze_chapter(llm, chapter, [t for _, t in topics])
    frame = proj["search_frame"]
    for tid, t in topics:
        ft = frame["topics"].get(tid, {})
        t["title_en"] = t.get("title_en") or ft.get("title_en", "")
        if force or "searchable" not in t:  # „trotzdem suchen“ der Autorin bleibt erhalten
            t["searchable"] = ft.get("searchable", True) is not False
    todo = [(tid, t) for tid, t in topics if t["searchable"]
            and (force or not (t.get("plan") or {}).get("queries"))]

    def work(t):
        plan = recommend.plan_search(llm, t, chapter, von, bis, frame)
        plan["queries"] = [q for q in plan.get("queries", []) if (q.get("query") or "").strip()]
        return plan

    for tid, res in _parallel(todo, work, log, "Suchplan").items():
        if isinstance(res, Exception):
            _err(proj, f"Suchplan {proj['topics'][tid]['title']}", res)
            continue
        t = proj["topics"][tid]
        t["plan"] = res
        t["plan_rev"] = t.get("plan_rev", 0) + 1
    recount_plan(proj, log)


def recount_plan(proj: dict, log=lambda m: None) -> None:
    """Trefferzahlen der (ggf. bearbeiteten) Suchen neu zählen – nur PubMed, keine KI-Kosten."""
    rules = proj.get("rules") or {}
    von, bis = recommend.search_window(rules, proj.get("search_window"))
    log("Treffer in PubMed werden gezählt…")
    for tid, t in _search_topics(proj):
        if t.get("plan") is not None:
            t["plan_counts"] = recommend.count_queries(
                recommend.with_manual(t["plan"], t.get("manual_query", ""))["queries"], von, bis, proj["email"])
    frame = proj.get("search_frame")
    if frame:
        frame["counts"] = recommend.count_queries(recommend.safety_queries(frame), von, bis, proj["email"])
    proj["plan_window"] = [von.isoformat(), bis.isoformat()]


def rank_failed(t: dict) -> bool:
    """Die Literaturrecherche dieses Themas ist fehlgeschlagen und noch nicht nachgeholt. Gilt auch für
    Zwischenstände, in denen ein älterer Programmstand dem Thema bereits eine leere Empfehlung eingetragen hat."""
    return bool(t.get("rec_error")) and "ranking" not in t


def run_recommendations(llm: LLM, proj: dict, log, batch: bool = False) -> None:
    """batch (Sparmodus): Die Bewertung der Suchtreffer und die Websuche nach Leitlinien laufen als ein
    Sammelauftrag zum halben Preis (siehe _rank_in_batch); alles andere bleibt unverändert."""
    apply_manual_limits(proj)
    proj["check_ok"] = False  # neue Auswahl: Themenzuordnung in Schritt 2 erneut prüfen
    chapter = (proj["toc"] or {}).get("chapter_title", "")
    rules = proj.get("rules") or {}
    n = rules.get("pubs_per_topic") or MAX_PUBS_PER_TOPIC
    window = proj.get("search_window")  # manuell eingestellt; bei Änderung werden die Themen neu gesucht
    frame = proj.get("search_frame")
    for tid, t in _search_topics(proj):
        if t.get("searchable") is False and "recommended" not in t:
            # nicht suchbare Überschrift (z.B. „Kuriosa“): wird über das Sicherheitsnetz abgedeckt
            t.update({"recommended": [], "reserve": [], "ranking": {}, "n_candidates": 0, "query_hits": {},
                      "window": list(proj.get("plan_window") or ["", ""]), "plan": {"queries": []}})
    def fresh(t):  # neu suchen und vollständig neu bewerten (auch: beim letzten Mal fehlgeschlagen)
        return ("recommended" not in t or t.get("n_candidates") == 0 or (window and t.get("window") != window)
                or t.get("plan_rev", 0) != t.get("ranked_rev", 0) or rank_failed(t))

    def widen(t):  # Thema mit bisher weniger Kandidaten als heute vorgesehen: nur die zusätzlichen bewerten
        return (t.get("cand_limit", MAX_CANDIDATES_PER_TOPIC) < recommend.candidate_limit(t.get("query_hits"))
                and t.get("n_candidates", 0) >= MAX_CANDIDATES_PER_TOPIC - 15)

    todo = [(tid, t) for tid, t in _search_topics(proj) if t.get("searchable") is not False
            and (fresh(t) or widen(t))]

    def known_of(t):  # bereits vorliegende Bewertungen (nur bei „breites Thema: zusätzliche Kandidaten“)
        if fresh(t):
            return None
        known = dict(t.get("ranking") or {})
        known.update({p: e["item"] for p, e in (t.get("prior_excluded") or {}).items()})
        return known

    def work(t):
        return recommend.recommend_for_topic(llm, t, chapter, rules, proj["email"], n, window=window,
                                             plan=t.get("plan"), frame=frame, known=known_of(t))

    def store(tid, res):
        if isinstance(res, Exception):
            _err(proj, f"Literaturempfehlung {proj['topics'][tid]['title']}", res)
            proj["topics"][tid]["rec_error"] = f"{type(res).__name__}: {res}"
            return
        t = proj["topics"][tid]
        t.pop("rec_error", None)
        t.pop("selected", None)  # alte Auswahl passt nicht zur neuen Trefferliste
        proj["records"].update(res.pop("records"))
        manual = t.get("manual_query", "")
        if t.get("plan") is None:  # ohne vorherigen Suchplan (Ein-Schritt-Ablauf): KI-Plan merken
            res["plan"] = {**res["plan"], "queries": [q for q in res["plan"].get("queries", [])
                                                      if q.get("name") != "eigene"]}
        else:
            res.pop("plan", None)  # den (ggf. bearbeiteten) Suchplan nicht überschreiben
        t.pop("prior_excluded", None)  # wird von apply_prior aus der neuen Rangliste neu bestimmt
        t.update(res)
        t["manual_query"] = manual
        t["ranked_rev"] = t.get("plan_rev", 0)

    if batch and hasattr(llm, "run_batch"):
        _rank_in_batch(llm, proj, log, todo, known_of, store, chapter, rules, n, window, frame)
    elif _step1_paid(llm, proj, todo):
        # Direkter Ablauf, aber aus einem Sammelauftrag liegt schon Bezahltes bereit: übernehmen, den Rest direkt
        log("Zu diesem Projekt gehört ein früherer Sammelauftrag. Seine Ergebnisse werden übernommen (ein noch "
            "laufender wird abgewartet); was dann fehlt, läuft direkt …")
        _rank_in_batch(llm, proj, log, todo, known_of, store, chapter, rules, n, window, frame, submit=False)
    else:
        for tid, res in _parallel(todo, work, log, "Literaturempfehlung").items():
            store(tid, res)
    apply_prior(proj, log, reselect=False)
    try:
        run_safety_net(llm, proj, log)
    except AccountError:
        raise
    except Exception as ex:
        _err(proj, "Sicherheitsnetz", ex)
    try:
        run_reroute(llm, proj, log)
    except AccountError:
        raise
    except Exception as ex:
        _err(proj, "Bewertung im passenden Thema", ex)
    try:
        run_followup(llm, proj, log)
    except AccountError:
        raise
    except Exception as ex:
        _err(proj, "Nachsuche nach Folgepublikationen", ex)
    try:
        classify_invasive(llm, proj, log)
    except AccountError:
        raise
    except Exception as ex:
        _err(proj, "Einordnung interventioneller Studien", ex)
    apply_prior(proj, log, reselect=False)
    select_chapter(proj, log)

    # Open-Access-Status der empfohlenen Arbeiten
    for t in proj["topics"].values():
        for pmid in t.get("recommended", []) + t.get("reserve", []):
            rec = proj["records"].get(pmid)
            if rec is not None and "is_oa" not in rec:
                info = unpaywall(rec.get("doi", ""), proj["email"])
                rec["is_oa"] = bool(info.get("is_oa"))
    fit_to_length(proj, log)


def _tidy_batch_state(proj: dict, kind: str) -> None:
    """Nach dem bloßen Abholen im direkten Ablauf: einen leer gebliebenen Merkzettel für Sammelaufträge wieder
    entfernen, damit ein Projekt ohne Sparmodus unverändert bleibt."""
    states = proj.get("batch")
    if isinstance(states, dict):
        if kind in states and not any(states[kind].values()):
            del states[kind]
        if not states:
            del proj["batch"]


def _step1_paid(llm, proj: dict, todo: list) -> bool:
    """Liegt für Schritt 1 etwas aus einem Sammelauftrag bereit, das noch nicht übernommen ist (bezahlte
    Bewertungen im Projekt oder ein eigener Sammelauftrag bei Anthropic)? Dann holt auch der direkte Ablauf es
    ab, statt die Bewertung noch einmal zu bezahlen."""
    if not hasattr(llm, "run_batch") or not (todo or _guideline_todo(proj)):
        return False
    if any((t.get("rank_cache") or {}).get("chunks") for _, t in todo):
        return True
    try:
        return llm.batch_waiting(f"{proj['id']}-s", (proj.get("batch") or {}).get("s"))
    except AccountError:  # bei Anthropic läuft ein Sammelauftrag unklarer Herkunft: anhalten, bevor bezahlt wird
        raise
    except Exception:  # noqa: BLE001 – die Nachfrage ist nur ein Sicherheitsnetz
        return False


def _rank_in_batch(llm, proj: dict, log, todo: list, known_of, store, chapter: str, rules: dict, n: int,
                   window, frame, submit: bool = True) -> None:
    """Sparmodus für Schritt 1 (2026-11-13): Die Bewertung der Suchtreffer (je 15 Abstracts ein Auftrag) und
    die Websuche nach Leitlinien gehen gemeinsam als EIN Sammelauftrag an die KI (halber Preis, Ergebnis meist
    innerhalb einer Stunde). Suchplan und PubMed-Suche davor sowie alles danach (Sicherheitsnetz, Zuordnung,
    Auswahl, Auslesen der Leitlinien) laufen wie gewohnt direkt. Die Aufträge sind dieselben wie im direkten
    Ablauf (recommend.rank_jobs, guidelines.search_job).

    Bezahlte Bewertungen stehen sofort im Projekt (t["rank_cache"], je Auftrag): Bricht der Lauf ab, bevor
    ein Thema vollständig ist, werden sie beim nächsten Start übernommen. Die Aufträge behalten dabei ihre
    Kennung, sodass auch noch nicht abgeholte Ergebnisse bei Anthropic wiedergefunden werden.

    submit=False (direkter Ablauf nach einem Lauf im Sparmodus): Es wird nur abgeholt, was schon bezahlt ist;
    die fehlenden Bewertungen laufen danach direkt (dieselben Aufträge), die fehlenden Leitliniensuchen
    erledigt run_guideline_search wie gewohnt direkt."""
    def sig(t):  # Suchplan und Zeitraum, zu denen bereits bezahlte Bewertungen gehören
        return [t.get("plan_rev", 0), list(window) if window else None]

    def prepare(t):
        return recommend.prepare_topic(llm, t, chapter, rules, proj["email"], window=window, plan=t.get("plan"),
                                       frame=frame, known=known_of(t))

    preps: dict[str, dict] = {}
    for tid, res in _parallel(todo, prepare, log, "PubMed-Suche").items():
        if isinstance(res, Exception):
            store(tid, res)
        else:
            preps[tid] = res
    jobs, owner = [], {}
    pending: dict[str, set] = {}
    rated: dict[str, dict] = {}
    failed: dict[str, Exception] = {}

    def finish(tid):
        if tid in failed:
            store(tid, failed[tid])  # die übrigen Bewertungen des Themas bleiben in t["rank_cache"] erhalten
            return
        store(tid, recommend.finish_topic(preps[tid], rated[tid], n))
        proj["topics"][tid].pop("rank_cache", None)

    for tid, prep in preps.items():
        t = proj["topics"][tid]
        cache = t.get("rank_cache") or {}
        if cache.get("for") != sig(t):
            cache = t["rank_cache"] = {"for": sig(t), "chunks": {}}
        chunks = recommend.rank_jobs(t, prep["plan"].get("pico", ""), prep["new"], prep["period"], key=f"r|{tid}")
        pending[tid], rated[tid] = set(), {}
        for job, valid in chunks:
            if job.key in cache.get("chunks", {}):  # schon bezahlt (unterbrochener Sammelauftrag)
                rated[tid].update(cache["chunks"][job.key])
                continue
            jobs.append(job)
            owner[job.key] = (tid, valid)
            pending[tid].add(job.key)
        if not pending[tid]:
            finish(tid)
    n_rank = len(jobs)
    for tid, t in _guideline_todo(proj):
        if not t.get("guidelines_raw"):
            job = G.search_job(t["title"], chapter, key=f"g|{tid}")
            jobs.append(job)
            owner[job.key] = (tid, None)
    if not jobs:
        return
    if submit:
        log(f"Sparmodus: {n_rank} Bewertungsaufträge und {len(jobs) - n_rank} Leitliniensuchen gehen als "
            "Sammelauftrag an die KI …")
    state = proj.setdefault("batch", {}).setdefault("s", {})

    def take(key, res):
        tid, valid = owner[key]
        t = proj["topics"][tid]
        if valid is None:  # Leitliniensuche: das Rechercheergebnis wertet run_guideline_search aus
            if isinstance(res, Exception):
                _err(proj, f"Leitlinien {t['title']} (Sammelauftrag; die Suche wird direkt nachgeholt)", res)
            elif res:
                t["guidelines_raw"] = res
            return
        if isinstance(res, Exception):
            failed.setdefault(tid, res)
        else:
            items = recommend.rank_items(res, valid)
            rated[tid].update(items)
            t["rank_cache"]["chunks"][key] = items  # sofort im Projekt: bezahlt ist bezahlt
        pending[tid].discard(key)
        if not pending[tid]:
            finish(tid)

    try:
        for key, res in llm.run_batch(jobs, prefix=f"{proj['id']}-s", state=state, log=log, submit=submit):
            if submit or not isinstance(res, Exception):  # direkter Ablauf: Gescheitertes läuft gleich direkt
                take(key, res)
    finally:
        if not submit:
            _tidy_batch_state(proj, "s")
    if submit:
        return
    rest = [(job.key, job) for job in jobs
            if owner[job.key][1] is not None and job.key in pending[owner[job.key][0]]]
    for key, res in _parallel(rest, lambda job: run_job(llm, job), log, "Bewertung der Suchtreffer").items():
        take(key, res)


SAFETY_MAX_CANDIDATES = 60


def apply_prior(proj: dict, log=lambda m: None, reselect: bool = True) -> int:
    """Arbeiten aus dem Vorjahresmanuskript (proj["prior"]) aus Empfehlung, Reserve und Auswahl nehmen und
    die Lücke aus der Rangliste auffüllen (ohne KI). Ausgeschlossene Arbeiten werden je Thema unter
    t["prior_excluded"] gemerkt; ohne Vorjahresmanuskript (oder nach dem Entfernen) kommen sie zurück.
    Gibt die Zahl der ausgeschlossenen Arbeiten zurück."""
    pr = proj.get("prior")
    rules = proj.get("rules") or {}
    n = rules.get("pubs_per_topic") or MAX_PUBS_PER_TOPIC
    total = set()
    for tid, t in proj["topics"].items():
        if "ranking" not in t:
            continue
        ex = t.setdefault("prior_excluded", {})
        changed = False
        for p in list(ex):  # zurückholen, was nicht mehr passt (z.B. Vorjahresmanuskript entfernt)
            if not prior.match(proj["records"].get(p, {}), pr):
                t["ranking"][p] = ex.pop(p)["item"]
                changed = True
        for p in list(t["ranking"]):
            why = prior.match(proj["records"].get(p, {}), pr)
            if why:
                ex[p] = {"item": t["ranking"].pop(p), "why": why}
                for k in ("recommended", "reserve", "selected"):
                    if p in t.get(k, []):
                        t[k] = [x for x in t[k] if x != p]
                changed = True
        if changed:
            if t.get("safety"):
                t["recommended"] = sorted(t["ranking"], key=lambda p: -t["ranking"][p].get("length_score", 0))
            else:
                t["recommended"], t["reserve"] = recommend.select(t["ranking"], n)
        if not ex:
            t.pop("prior_excluded", None)
        total.update(ex)
    proj["prior_warning"] = prior.warning(pr, len(total), proj.get("search_window") or proj.get("plan_window"))
    if pr:
        log(f"Vorjahresmanuskript: {len(total)} bereits gewürdigte oder zitierte Arbeiten ausgeschlossen.")
        if proj["prior_warning"]:
            log("⚠️ " + proj["prior_warning"])
    if reselect:
        select_chapter(proj, log)
    return len(total)


def run_safety_net(llm: LLM, proj: dict, log) -> None:
    """Kapitelweite Suchen (hohe Evidenz, Top-Journale, Anker allein). Neue Treffer, die in keiner Themensuche
    vorkamen, werden für das ganze Kapitel bewertet. Arbeiten der Priorität A werden dem passenden Thema
    zugeordnet; passen sie zu keinem, landen sie unter „Neue Themen außerhalb des Inhaltsverzeichnisses“
    (zunächst nicht im Manuskript, die Autorin entscheidet)."""
    frame = proj.get("search_frame")
    queries = recommend.safety_queries(frame or {})
    if not queries:
        return
    rules = proj.get("rules") or {}
    von, bis = recommend.search_window(rules, proj.get("search_window"))
    topics = _search_topics(proj)
    key = json.dumps([[q["query"] for q in queries], von.isoformat(), bis.isoformat(), sorted(tid for tid, _ in topics)])
    net = proj.get("safety_net") or {}
    if net.get("key") != key:
        known = set()
        for tid, t in proj["topics"].items():
            if tid != SAFETY:
                known.update(p for p, it in t.get("ranking", {}).items() if it.get("via") != "Sicherheitsnetz")
                known.update(t.get("prior_excluded", {}))
        log("Sicherheitsnetz: kapitelweite Suche nach weiteren wichtigen Arbeiten…")
        pmids, stats = recommend.collect_candidates({"queries": queries}, von, bis, proj["email"],
                                                    limit=SAFETY_MAX_CANDIDATES * 3)
        new = [p for p in pmids if p not in known][:SAFETY_MAX_CANDIDATES]
        records = pubmed.fetch_records(new, proj["email"]) if new else {}
        cands = recommend.prefilter([records[p] for p in new if p in records
                                     and not prior.match(records[p], proj.get("prior"))])
        chapter = (proj["toc"] or {}).get("chapter_title", "")
        pico = "Alle Themen des Kapitels: " + "; ".join(recommend.topic_path(t) for _, t in topics)
        ranking = recommend.rank(llm, {"title": f"{chapter} (gesamtes Kapitel)"}, pico, cands,
                                 f"von {von.strftime('%m/%Y')} bis {bis.strftime('%m/%Y')}") if cands else {}
        recommend.select(ranking, 10 ** 6)  # berechnet Priorität und Längenwert
        top = [p for p, it in ranking.items() if it.get("priority") == "A"]
        assign = {}
        if top:
            topics_block = "\n".join(f"{tid}: {t.get('number', '')} {recommend.topic_path(t)}" for tid, t in topics)
            pubs_block = "\n".join(f"{p}: {records[p]['title']} – "
                                   f"{' '.join(records[p].get('abstract', '').split())[:500]}" for p in top)
            data = llm.json(P.topic_assign_prompt(topics_block, pubs_block), ASSIGN_SCHEMA, max_tokens=4000,
                            effort="low")
            valid = {tid for tid, _ in topics}
            assign = {a["key"]: a["topic_id"] for a in data.get("assignments", []) if a.get("topic_id") in valid}
        proj["records"].update({p: records[p] for p in top if p in records})
        net = {"key": key, "hits": stats, "n_new": len(cands), "window": [von.isoformat(), bis.isoformat()],
               "items": {p: {"topic": assign.get(p, ""), "item": {**ranking[p], "via": "Sicherheitsnetz"}} for p in top}}
        proj["safety_net"] = net
    # Zuordnung anwenden (auch nach einer erneuten Themensuche, ohne neue Kosten)
    n = rules.get("pubs_per_topic") or MAX_PUBS_PER_TOPIC
    valid = {tid for tid, _ in topics}
    orphans = []
    for p, e in net.get("items", {}).items():
        tid = e["topic"]
        if tid not in valid:
            orphans.append(p)
            continue
        t = proj["topics"][tid]
        if p not in t.setdefault("ranking", {}):
            t["ranking"][p] = e["item"]
            t["recommended"], t["reserve"] = recommend.select(t["ranking"], n)
    if orphans:
        sf = proj["topics"].setdefault(SAFETY, {
            "id": SAFETY, "title": "Neue Themen außerhalb des Inhaltsverzeichnisses", "number": "",
            "english_search_focus": "", "section_number": "Neue Themen",
            "section_title": "Neue Themen außerhalb des Inhaltsverzeichnisses", "enabled": False, "safety": True,
            "guidelines": []})
        if set(sf.get("ranking", {})) != set(orphans):
            sf.pop("selected", None)
        sf["ranking"] = {p: net["items"][p]["item"] for p in orphans}
        sf["recommended"] = sorted(orphans, key=lambda p: -sf["ranking"][p].get("length_score", 0))
        sf["reserve"] = []
        sf.update({"window": net.get("window", ["", ""]), "n_candidates": net.get("n_new", 0),
                   "query_hits": net.get("hits", {}), "plan": {"queries": queries}})
    else:
        proj["topics"].pop(SAFETY, None)
    net["assigned"], net["orphans"] = len(net.get("items", {})) - len(orphans), len(orphans)
    log(f"Sicherheitsnetz: {net.get('n_new', 0)} weitere Arbeiten bewertet, {net['assigned']} einem Thema "
        f"zugeordnet, {len(orphans)} als mögliches neues Thema.")


# ----------------------------------------------------------------------
# Kapitelweite Auswahl (Angelika 2026-11-09, alle sechs Vorschläge der Analyse pAVK konservativ)
# ----------------------------------------------------------------------

CHAPTER_MIN_SCORE = 45   # relative Empfehlung: darunter wird auch bei freier Zielzahl nichts empfohlen
SHEET_MIN_ITEMS = 8      # Auswahlliste: so viele Arbeiten je Thema mindestens zeigen (Empfehlung + Reserve)
SHEET_MIN_RELEVANCE = 40  # Auffüllen der Liste nur mit Arbeiten, die zum Thema passen
BACKGROUND_MAX = 5       # Hintergrundliteratur je Thema
BACKGROUND_MIN_RELEVANCE = 70
REROUTE_MAX = 80         # so viele gute Arbeiten höchstens in einem passenderen Thema nachbewerten
REROUTE_MIN_EVIDENCE = 55
REROUTE_MAX_RELEVANCE = 70  # gilt für die beste bisherige Relevanz der Arbeit


def _best_topics(proj: dict) -> dict[str, tuple[str, dict]]:
    """{pmid: (Thema, Bewertung)} – jedes Thema, in dem eine Arbeit am besten passt (höchste Relevanz,
    bei Gleichstand höherer Score). Berechnet dabei Score, Priorität und Längenwert aller Bewertungen."""
    best: dict[str, tuple[str, dict]] = {}
    for tid, t in _search_topics(proj):
        rk = t.get("ranking") or {}
        recommend.select(rk, 0)
        for p, it in rk.items():
            cur = best.get(p)
            if cur is None or (it.get("relevance", 0), it.get("score", 0)) > \
                    (cur[1].get("relevance", 0), cur[1].get("score", 0)):
                best[p] = (tid, it)
    return best


def select_chapter(proj: dict, log=lambda m: None) -> None:
    """Empfehlung kapitelweit und relativ: Die besten Originalarbeiten des Kapitels werden bis zur Zielzahl
    (Höchstzahl gewürdigter Publikationen bzw. Summe der Würdigungslängen) empfohlen, Priorität A zuerst,
    dann nach Score. Jede Arbeit steht nur in dem Thema, zu dem sie am besten passt. Je Thema bleiben die
    Obergrenze (Hinweise für Autoren, sonst 6) und höchstens 3 Arbeiten je Fragestellung.
    Reserve: alle weiteren A- und B-Arbeiten des Themas, aufgefüllt auf mindestens 8 Einträge.
    Hintergrundliteratur: passende Leitlinien, Konsensuspapiere und Reviews (werden nicht gewürdigt)."""
    from .selection_sheet import target_count
    topics = _search_topics(proj)
    if not any(t.get("ranking") for _, t in topics):
        return
    rules = proj.get("rules") or {}
    n_topic = rules.get("pubs_per_topic") or MAX_PUBS_PER_TOPIC
    target = target_count(proj)[0] or n_topic * len(topics)
    best = _best_topics(proj)
    primary = {p: v for p, v in best.items() if v[1].get("is_primary_research", True)}
    inv = invasive_set(proj)  # Schalter „Interventionelle Studien abwerten“: nur noch Reserve
    eligible = [p for p, (_, it) in primary.items() if it.get("relevance", 0) >= recommend.RELEVANCE_MIN
                and (it.get("priority") == "A" or it.get("score", 0) >= CHAPTER_MIN_SCORE) and p not in inv]
    eligible.sort(key=lambda p: (primary[p][1].get("priority") == "A", primary[p][1].get("score", 0),
                                 primary[p][1].get("length_score", 0)), reverse=True)
    rec: dict[str, list[str]] = {tid: [] for tid, _ in topics}
    per_key: dict[tuple, int] = {}
    for p in eligible:
        if sum(len(v) for v in rec.values()) >= target:
            break
        tid, it = primary[p]
        key = (tid, (it.get("question_key") or p).strip().lower())
        if len(rec[tid]) >= n_topic or per_key.get(key, 0) >= recommend.MAX_PER_QUESTION:
            continue
        rec[tid].append(p)
        per_key[key] = per_key.get(key, 0) + 1
    taken = {p for v in rec.values() for p in v}
    changed = 0
    for tid, t in topics:
        if "ranking" not in t:
            # Recherche noch offen oder fehlgeschlagen: nichts eintragen. Eine leere Empfehlung sähe wie ein
            # fertig recherchiertes Thema aus; es würde dann beim nächsten Start nicht wiederholt.
            continue
        rk = t.get("ranking") or {}
        mine = sorted(rec[tid], key=lambda p: (rk[p].get("length_score", 0), rk[p].get("score", 0)), reverse=True)
        own = [p for p, (b, _) in primary.items() if b == tid and p not in taken]
        own.sort(key=lambda p: (p not in inv, rk[p].get("priority") in ("A", "B"), rk[p].get("score", 0)),
                 reverse=True)
        reserve = [p for p in own if rk[p].get("priority") in ("A", "B")
                   and rk[p].get("relevance", 0) >= recommend.RELEVANCE_MIN]
        for p in own:
            if len(mine) + len(reserve) >= SHEET_MIN_ITEMS:
                break
            if p not in reserve and rk[p].get("relevance", 0) >= SHEET_MIN_RELEVANCE:
                reserve.append(p)
        old = t.get("recommended", [])
        if t.get("selected") is not None and list(t["selected"]) == list(old):
            t.pop("selected")  # unverändert übernommene Empfehlung folgt der neuen Empfehlung
        if mine != old:
            changed += 1
        t["recommended"], t["reserve"] = mine, reserve
        bg = [p for p, (b, it) in best.items() if b == tid and not it.get("is_primary_research", True)
              and it.get("relevance", 0) >= BACKGROUND_MIN_RELEVANCE]
        bg.sort(key=lambda p: (rk[p].get("relevance", 0), rk[p].get("score", 0)), reverse=True)
        t["background"] = bg[:BACKGROUND_MAX]
    if changed:
        bump_selection(proj)
    n_rec = len(taken)
    n_a = sum(1 for p in taken if primary[p][1].get("priority") == "A")
    log(f"Kapitelweite Auswahl: {n_rec} von {target} Empfehlungen ({n_a} mit Priorität A, die übrigen nach "
        f"Score), Reserve und Hintergrundliteratur je Thema aktualisiert."
        + (f" {len(inv)} interventionelle Arbeit(en) stehen nur in der Reserve." if inv else ""))


INVASIVE_SCHEMA = obj({"items": arr(obj({"key": STR, "invasive": BOOL}))})
TRIAL_SCHEMA = obj({"items": arr(obj({"key": STR, "trial": STR}))})
FOLLOWUP_PER_TRIAL = 15  # PubMed-Treffer je Studienname
FOLLOWUP_MAX = 60        # so viele neue Folgepublikationen höchstens bewerten
FOLLOWUP_MIN_SCORE = 60  # Ausgangsarbeiten: Priorität A oder Score ab 60


def invasive_set(proj: dict) -> set[str]:
    """Arbeiten, die bei eingeschaltetem Schalter „Interventionelle Studien abwerten“ nicht empfohlen werden."""
    if not proj.get("focus_conservative"):
        return set()
    return {p for p, v in (proj.get("invasive") or {}).items() if v}


def invasive_pending(proj: dict) -> list[str]:
    """Arbeiten, die für die Auswahlliste in Frage kommen und noch nicht als interventionell/nicht
    interventionell eingeordnet sind."""
    done = proj.get("invasive") or {}
    return [p for p, (_, it) in _best_topics(proj).items() if p not in done and p in proj["records"]
            and it.get("is_primary_research", True) and it.get("relevance", 0) >= SHEET_MIN_RELEVANCE]


def classify_invasive(llm: LLM, proj: dict, log=lambda m: None) -> int:
    """Nur bei eingeschaltetem Schalter: ordnet die in Frage kommenden Arbeiten als interventionell oder
    nicht interventionell ein (schnelles Modell, 40 Arbeiten je Anfrage, Ergebnis je Arbeit gemerkt).
    Angelika 2026-11-10: nur sinnvoll, wenn es zum Thema ein eigenes interventionelles Kapitel gibt."""
    if not proj.get("focus_conservative"):
        return 0
    todo = invasive_pending(proj)
    if not todo:
        return 0
    log(f"{len(todo)} Arbeiten werden als interventionell oder konservativ eingeordnet…")
    chapter = (proj.get("toc") or {}).get("chapter_title", "")
    out = proj.setdefault("invasive", {})
    for i in range(0, len(todo), 40):
        part = todo[i:i + 40]
        block = "\n".join(f"{p}: {proj['records'][p].get('title', '')} – "
                           f"{' '.join((proj['records'][p].get('abstract') or '').split())[:400]}" for p in part)
        data = llm.json(P.invasive_prompt(chapter, block), INVASIVE_SCHEMA, model=MODEL_FAST,
                        max_tokens=4000, effort="low")
        out.update({a["key"]: bool(a.get("invasive")) for a in data.get("items", []) if a.get("key") in part})
    n = sum(1 for p in todo if out.get(p))
    log(f"{n} von {len(todo)} Arbeiten sind interventionell.")
    return n


def _followup_seeds(proj: dict) -> list[str]:
    seeds = []
    for p, (_, it) in _best_topics(proj).items():
        if p in proj["records"] and it.get("is_primary_research", True) \
                and (it.get("priority") == "A" or it.get("score", 0) >= FOLLOWUP_MIN_SCORE):
            seeds.append(p)
    return seeds


def followup_pending(proj: dict) -> int:
    """Zahl der guten Arbeiten, deren Studienname noch nicht nachgesucht wurde."""
    fu = proj.get("followup") or {}
    if fu.get("window") != list(proj.get("search_window") or proj.get("plan_window") or []):
        fu = {}
    seen = set(fu.get("seeds") or {})
    return sum(1 for p in _followup_seeds(proj) if p not in seen)


def run_followup(llm: LLM, proj: dict, log) -> None:
    """Nachsuche nach Folgepublikationen großer Studien (Angelika 2026-11-10, nach dem Vergleich pAVK
    interventionell): Für gute Originalarbeiten wird der Studienname bestimmt (z. B. BEST-CLI, BASIL-2) und
    im Suchzeitraum in PubMed gesucht. Neue Treffer werden dem passendsten Thema zugeordnet und dort bewertet."""
    topics = _search_topics(proj)
    if not topics:
        return
    valid = {tid for tid, _ in topics}
    win = list(proj.get("search_window") or proj.get("plan_window") or [])
    fu = proj.get("followup") or {}
    if fu.get("window") != win:
        fu = {"window": win, "seeds": {}, "trials": {}}
    proj["followup"] = fu
    seeds = [p for p in _followup_seeds(proj) if p not in fu["seeds"]]
    for i in range(0, len(seeds), 40):
        part = seeds[i:i + 40]
        block = "\n".join(f"{p}: {proj['records'][p].get('title', '')} – "
                           f"{' '.join((proj['records'][p].get('abstract') or '').split())[:500]}" for p in part)
        data = llm.json(P.trial_names_prompt(block), TRIAL_SCHEMA, model=MODEL_FAST, max_tokens=4000, effort="low")
        names = {a["key"]: (a.get("trial") or "").strip() for a in data.get("items", []) if a.get("key") in part}
        for p in part:
            fu["seeds"][p] = names.get(p, "")
    rules = proj.get("rules") or {}
    von, bis = recommend.search_window(rules, proj.get("search_window"))
    suffix = recommend._suffix(von, bis)
    for name in sorted({n for n in fu["seeds"].values() if len(n) >= 3}):
        if name in fu["trials"]:
            continue
        q = name.replace('"', "")
        try:
            ids, _ = pubmed.search(f'("{q}"[tiab]){suffix}', FOLLOWUP_PER_TRIAL, proj["email"])
        except Exception as ex:  # ein fehlerhafter Name soll die Nachsuche nicht abbrechen
            _err(proj, f"Nachsuche {name}", ex)
            continue
        fu["trials"][name] = ids
    ranked = {p for _, t in topics for p in (t.get("ranking") or {})} | \
             {p for _, t in topics for p in (t.get("prior_excluded") or {})}
    new = list(dict.fromkeys(p for ids in fu["trials"].values() for p in ids if p not in ranked))[:FOLLOWUP_MAX]
    if not new:
        log(f"Nachsuche nach Folgepublikationen: {len(fu['trials'])} Studiennamen, keine neuen Arbeiten.")
        return
    records = pubmed.fetch_records(new, proj["email"])
    cands = [r["pmid"] for r in recommend.prefilter([records[p] for p in new if p in records])]
    if not cands:
        return
    log(f"Nachsuche nach Folgepublikationen: {len(cands)} neue Arbeiten zu {len(fu['trials'])} Studien "
        f"werden zugeordnet und bewertet…")
    proj["records"].update({p: records[p] for p in cands})
    topics_block = "\n".join(f"{tid}: {t.get('number', '')} {recommend.topic_path(t)}" for tid, t in topics)
    assign: dict[str, str] = {}
    for i in range(0, len(cands), 40):
        part = cands[i:i + 40]
        pubs_block = "\n".join(f"{p}: {records[p].get('title', '')} – "
                                f"{' '.join((records[p].get('abstract') or '').split())[:400]}" for p in part)
        data = llm.json(P.topic_assign_prompt(topics_block, pubs_block), ASSIGN_SCHEMA, model=MODEL_FAST,
                        max_tokens=4000, effort="low")
        assign.update({a["key"]: a["topic_id"] for a in data.get("assignments", []) if a.get("key") in part})
    by_topic: dict[str, list[str]] = {}
    for p in cands:
        tid = assign.get(p, "")
        if tid in valid:
            by_topic.setdefault(tid, []).append(p)
    period = f"von {von.strftime('%m/%Y')} bis {bis.strftime('%m/%Y')}"

    def work(item):
        tid, pmids = item
        t = proj["topics"][tid]
        return recommend.rank(llm, t, (t.get("plan") or {}).get("pico", ""), [records[p] for p in pmids], period)

    res = _parallel([(tid, (tid, pm)) for tid, pm in by_topic.items()], work, log, "Bewertung der Folgepublikationen")
    n = 0
    for tid, items in res.items():
        if isinstance(items, Exception):
            _err(proj, f"Bewertung der Folgepublikationen {proj['topics'][tid]['title']}", items)
            continue
        for p, it in items.items():
            proj["topics"][tid].setdefault("ranking", {})[p] = {**it, "via": "Folgepublikation"}
            proj.setdefault("reroute", {})[p] = tid  # nicht noch einmal umsortieren
            fu["seeds"][p] = next((nm for nm, ids in fu["trials"].items() if p in ids), "")  # schon nachgesucht
            n += 1
    log(f"{n} Folgepublikationen bewertet.")


def extension_pending(proj: dict) -> dict:
    """Was ein erneutes „Literatur bewerten“ in einem schon bewerteten Projekt nachholen würde."""
    widen = [tid for tid, t in _search_topics(proj) if "recommended" in t and t.get("searchable") is not False
             and t.get("cand_limit", MAX_CANDIDATES_PER_TOPIC) < recommend.candidate_limit(t.get("query_hits"))
             and t.get("n_candidates", 0) >= MAX_CANDIDATES_PER_TOPIC - 15]
    return {"widen": widen, "followup": followup_pending(proj) if any(t.get("ranking") for _, t in
                                                                       _search_topics(proj)) else 0}


def run_reroute(llm: LLM, proj: dict, log) -> None:
    """Gute Originalarbeiten, die in keinem ihrer bisherigen Themen gut passen (Relevanz unter 70), werden dem
    am besten passenden Thema zugeordnet und dort zusätzlich bewertet (z. B. eine Inclisiran-Studie, die nur
    die Suche zu Lp(a) gefunden hat, unter PCSK9-Inhibition). Ergebnisse werden je Arbeit gemerkt."""
    topics = _search_topics(proj)
    valid = {tid for tid, _ in topics}
    best = _best_topics(proj)
    done = proj.setdefault("reroute", {})
    cands = [p for p, (_, it) in best.items() if p not in done and it.get("is_primary_research", True)
             and it.get("evidence", 0) >= REROUTE_MIN_EVIDENCE and it.get("relevance", 0) < REROUTE_MAX_RELEVANCE
             and p in proj["records"]]
    cands.sort(key=lambda p: -best[p][1].get("evidence", 0))
    cands = cands[:REROUTE_MAX]
    if not cands:
        return
    log(f"{len(cands)} gute Arbeiten werden dem passendsten Thema zugeordnet…")
    topics_block = "\n".join(f"{tid}: {t.get('number', '')} {recommend.topic_path(t)}" for tid, t in topics)
    assign: dict[str, str] = {}
    for i in range(0, len(cands), 40):
        part = cands[i:i + 40]
        pubs_block = "\n".join(f"{p}: {proj['records'][p].get('title', '')} – "
                                f"{' '.join((proj['records'][p].get('abstract') or '').split())[:400]}" for p in part)
        data = llm.json(P.topic_assign_prompt(topics_block, pubs_block), ASSIGN_SCHEMA, model=MODEL_FAST,
                        max_tokens=4000, effort="low")
        assign.update({a["key"]: a["topic_id"] for a in data.get("assignments", []) if a.get("key") in part})
    by_topic: dict[str, list[str]] = {}
    for p in cands:
        tid = assign.get(p, "")
        done[p] = tid if tid in valid else ""
        if tid in valid and p not in (proj["topics"][tid].get("ranking") or {}) \
                and p not in (proj["topics"][tid].get("prior_excluded") or {}):
            by_topic.setdefault(tid, []).append(p)
    if not by_topic:
        return
    rules = proj.get("rules") or {}
    von, bis = recommend.search_window(rules, proj.get("search_window"))
    period = f"von {von.strftime('%m/%Y')} bis {bis.strftime('%m/%Y')}"

    def work(item):
        tid, pmids = item
        t = proj["topics"][tid]
        return recommend.rank(llm, t, (t.get("plan") or {}).get("pico", ""),
                              [proj["records"][p] for p in pmids], period)

    res = _parallel([(tid, (tid, pm)) for tid, pm in by_topic.items()], work, log, "Bewertung im passenden Thema")
    n = 0
    for tid, items in res.items():
        if isinstance(items, Exception):
            _err(proj, f"Bewertung im passenden Thema {proj['topics'][tid]['title']}", items)
            for p in by_topic[tid]:
                done.pop(p, None)
            continue
        for p, it in items.items():
            proj["topics"][tid].setdefault("ranking", {})[p] = {**it, "via": "Themenzuordnung"}
            n += 1
    log(f"{n} Arbeiten zusätzlich im passenden Thema bewertet.")


def _enabled_topics(proj: dict) -> list[str]:
    return [tid for tid, t in proj["topics"].items() if t.get("enabled", True)]


def _rank_item(proj: dict, p: str, t: str) -> dict:
    """Bewertung aus Schritt 1; für eine gemeinsame Würdigung die beste der beteiligten Publikationen."""
    rk = proj["topics"][t].get("ranking", {})
    members = (proj["pubs"].get(p) or {}).get("group")
    if members:
        items = [rk.get(m, {}) for m in members]
        return max(items, key=lambda it: it.get("length_score", 0), default={})
    return rk.get(p, {})


def _length_info(proj: dict, pairs: list[tuple[str, str]]) -> list[dict]:
    return [{"id": p, "length_score": _rank_item(proj, p, t).get("length_score", 50)} for p, t in pairs]


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


def _guideline_todo(proj: dict) -> list[tuple[str, dict]]:
    return [(tid, t) for tid, t in proj["topics"].items() if t.get("enabled", True) and "guidelines" not in t
            and not t.get("safety")]


def run_guideline_search(llm: LLM, proj: dict, log) -> None:
    chapter = (proj["toc"] or {}).get("chapter_title", "")
    todo = _guideline_todo(proj)

    def work(t):
        raw = t.get("guidelines_raw")  # Rechercheergebnis aus dem Sammelauftrag (Sparmodus); sonst direkte Suche
        found = G.finish_search(llm, raw) if raw else G.search_guidelines(llm, t["title"], chapter)
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
        proj["topics"][tid].pop("guidelines_raw", None)


GL_NEED_SCHEMA = obj({"requests": arr(obj({"index": {"type": "integer"}, "reason": STR}))})
GL_NEED_MAX = 2  # höchstens so viele Leitlinien je Unterkapitel bei den Autoren anfragen


def _gl_need_key(t: dict) -> str:
    pubs = t.get("selected", t.get("recommended", []))
    gls = [(g.get("title", ""), bool(g.get("text"))) for g in t.get("guidelines") or []]
    return hashlib.sha1(json.dumps([sorted(pubs), gls], ensure_ascii=False).encode()).hexdigest()[:16]


def run_guideline_needs(llm: LLM, proj: dict, log, force: bool = False) -> None:
    """Bestimmt je Unterkapitel, welche (nicht frei geladenen) Leitlinien die Würdigung der ausgewählten
    Publikationen inhaltlich verbessern würden. Nur diese werden in der Auswahlliste bei den Autoren
    angefragt. Ein schneller Aufruf ohne Websuche je Unterkapitel (wenige Cent)."""
    todo = []
    for tid, t in proj["topics"].items():
        if not t.get("enabled", True):
            continue
        missing = [i for i, g in enumerate(t.get("guidelines") or []) if not g.get("text")]
        pubs = t.get("selected", t.get("recommended", []))
        key = _gl_need_key(t)
        if not missing or not pubs:
            t["gl_need"] = {"key": key, "requests": []}
        elif force or (t.get("gl_need") or {}).get("key") != key:
            todo.append((tid, t))

    def work(t):
        gls = t.get("guidelines") or []
        g_block = "\n".join(
            f"[{i}] {g.get('title', '')} ({g.get('society', '')}, {g.get('year', '')}) – "
            + ("liegt der KI bereits im Volltext vor" if g.get("text") else "NICHT verfügbar")
            + (f". Relevanz laut Recherche: {g.get('relevance')}" if g.get("relevance") else "")
            for i, g in enumerate(gls))
        p_block = "\n".join(f"- {(proj['records'].get(p) or {}).get('title', p)}"
                             for p in t.get("selected", t.get("recommended", [])))
        res = llm.json(P.guideline_need_prompt(t["title"], p_block, g_block, GL_NEED_MAX), GL_NEED_SCHEMA,
                       model=MODEL_FAST, max_tokens=1500, effort="low")
        out, seen = [], set()
        for r in res.get("requests", []):
            i = r.get("index")
            if isinstance(i, int) and 0 <= i < len(gls) and not gls[i].get("text") and i not in seen:
                seen.add(i)
                out.append({"index": i, "reason": (r.get("reason") or "").strip()})
        return {"key": _gl_need_key(t), "requests": out[:GL_NEED_MAX]}

    if not todo:
        return
    for tid, res in _parallel(todo, work, log, "Benötigte Leitlinien bestimmen").items():
        if isinstance(res, Exception):
            _err(proj, f"Leitlinienbedarf {proj['topics'][tid]['title']}", res)
        else:
            proj["topics"][tid]["gl_need"] = res


def recommendation_docx(proj: dict) -> bytes:
    rankings = {}
    for t in proj["topics"].values():
        rankings.update(t.get("ranking", {}))
    by_topic: dict[str, list[str]] = {}
    for p, tid in _pub_list(proj):  # aktuelle Auswahl, jede Publikation nur einmal
        by_topic.setdefault(tid, []).append(p)
    topics = [{"title": f"{t.get('number', '')} {t['title']}".strip(), "pmids": by_topic.get(tid, []),
               "background": t.get("background", [])}
              for tid, t in proj["topics"].items() if t.get("enabled", True)]
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
        if name in proj["uploads"]:  # bereits zugeordnet: Text und Zuordnung bleiben
            continue
        text = extract_pdf_text(data)
        hit = next((p for p, rec in wanted.items() if upload_matches(text, rec)), None)
        proj["uploads"][name] = {"pmid": hit, "text_len": len(text)}
        if hit:
            proj["pubs"].setdefault(hit, {})["full_text"] = text
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
        proj["pubs"].setdefault(pmid, {}).update({"full_text": text, "extra": True})
        extra.append(pmid)

    if extra:
        topics_block = "\n".join(f"{t['id']}: {t['title']}" for t in proj["topics"].values() if t.get("enabled", True))
        pubs_block = "\n".join(
            f"{p}: {proj['records'].get(p, {}).get('title', '')} – "
            + " ".join((proj["records"].get(p, {}).get("abstract") or proj["pubs"][p].get("full_text", "")[:600]).split())[:500]
            for p in extra)
        try:
            data = llm.json(P.topic_assign_prompt(topics_block, pubs_block), ASSIGN_SCHEMA, max_tokens=3000,
                            effort="low")
            for a in data.get("assignments", []):
                if a["key"] in proj["pubs"] and a["topic_id"] in proj["topics"]:
                    proj["pubs"][a["key"]]["topic_id"] = a["topic_id"]
        except AccountError:
            raise
        except Exception as ex:
            _err(proj, "Themenzuordnung", ex)
        ensure_varia(proj)


def attach_direct_upload(proj: dict, p: str, name: str, data: bytes, force: bool = False) -> dict:
    """PDF direkt bei einer Publikation der Literaturliste hochgeladen: wie in MyLuo kurz prüfen, ob es die
    richtige Publikation ist (DOI oder Titel plus Erstautor im Kopfbereich). Ohne KI, daher kostenlos.
    Passt sie nicht, wird sie nur mit force=True übernommen. Rückgabe: {"ok", "match", "found"}."""
    text = extract_pdf_text(data)
    rec = proj["records"].get(p, {})
    match = upload_matches(text, rec)
    found = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) > 25), "")[:160]
    if not (match or force):
        return {"ok": False, "match": False, "found": found, "empty": not text.strip()}
    d = proj["pubs"].setdefault(p, {})
    d.update({"full_text": text, "fulltext_source": "Upload", "upload_name": name,
              "upload_checked": "passt" if match else "von Ihnen bestätigt"})
    d.pop("images", None)
    proj["uploads"][name] = {"pmid": p, "text_len": len(text)}
    return {"ok": True, "match": match, "found": found, "empty": not text.strip()}


VARIA = "VARIA"


def topic_order(proj: dict) -> list[str]:
    """Themen in Manuskript-Reihenfolge: wie im (kumulativen) Inhaltsverzeichnis, danach „Neue Themen“
    (Sicherheitsnetz) und ganz am Ende „Varia“ (Entscheidung der Autorin 2026-11-13)."""
    tail = [t for t in (SAFETY, VARIA) if t in proj["topics"]]
    return [t for t in proj["topics"] if t not in tail] + tail


def ensure_varia(proj: dict) -> None:
    """Hochgeladene Publikationen, die zu keinem Thema des Inhaltsverzeichnisses passen, kommen unter
    den Inhaltspunkt „Varia“ (eigener, letzter Abschnitt ohne Literaturrecherche und ohne State of the Art)."""
    orphans = [p for p, d in proj["pubs"].items() if d.get("extra") and not d.get("group")
               and (d.get("topic_id") or "") not in proj["topics"]]
    if not orphans:
        return
    if VARIA not in proj["topics"]:
        proj["topics"][VARIA] = {"id": VARIA, "title": "Varia", "number": "", "english_search_focus": "",
                                 "section_number": "Varia", "section_title": "Varia", "enabled": True,
                                 "varia": True, "recommended": [], "reserve": [], "ranking": {}, "guidelines": [],
                                 "window": ["", ""], "n_candidates": 0}
    for p in orphans:
        proj["pubs"][p]["topic_id"] = VARIA


REASSIGN_SCHEMA = obj({"assignments": arr(obj({"pub_id": STR, "topic_id": STR, "reason_de": STR})),
                       "same_question": arr(obj({"pub_ids": arr(STR), "question_de": STR}))})


def current_topics(proj: dict) -> dict[str, str]:
    """{pmid: topic_id} aller Publikationen, die ins Manuskript kommen (ohne gemeinsame Würdigungen)."""
    return {p: t for p, t in _pub_list(proj) if not proj["pubs"].get(p, {}).get("group")}


def bump_selection(proj: dict) -> None:
    """Die Auswahl wurde vom Programm geändert: die Ankreuzfelder in Schritt 1 übernehmen den neuen Stand."""
    proj["sel_rev"] = proj.get("sel_rev", 0) + 1


def _unselect_everywhere(proj: dict, p: str, keep: str = "") -> None:
    """Dieselbe Arbeit kann in Schritt 1 für mehrere Themen empfohlen worden sein."""
    for tid, t in proj["topics"].items():
        if tid != keep and p in t.get("selected", t.get("recommended", [])):
            t["selected"] = [x for x in t.get("selected", t.get("recommended", [])) if x != p]


def move_pub(proj: dict, p: str, dst: str) -> None:
    """Verschiebt eine Publikation in ein anderes Thema (die Bewertung aus Schritt 1 wandert mit)."""
    src = current_topics(proj).get(p)
    if not src or src == dst or dst not in proj["topics"]:
        return
    d = proj["pubs"].setdefault(p, {})
    d.setdefault("home_topic", src)
    bump_selection(proj)
    if d.get("extra"):
        d["topic_id"] = dst
        return
    s_t, d_t = proj["topics"][src], proj["topics"][dst]
    _unselect_everywhere(proj, p, keep=dst)
    d_t["selected"] = list(d_t.get("selected", d_t.get("recommended", []))) + [p]
    if p in s_t.get("ranking", {}):
        d_t.setdefault("ranking", {})[p] = s_t["ranking"][p]


def _reassign_key(proj: dict) -> list:
    return ([f"v{GROUP_VERSION}"] + sorted(current_topics(proj))
            + sorted(f"{p}={t}" for p, t in (proj.get("topic_manual") or {}).items()))


def reassign_topics(llm: LLM, proj: dict, log, force: bool = False) -> None:
    """Prüft nach dem Hochladen die Themenzuordnung ALLER Publikationen im Gesamtblick: Schwerpunkt der
    Arbeit entscheidet (klinisch → Krankheitsbild, technischer Aspekt → Verfahren); Arbeiten mit nahezu
    gleicher Fragestellung kommen ins selbe Thema und werden als Gruppe vorgemerkt."""
    ensure_varia(proj)
    _restore_limit_dropped(proj)  # wegen der Vorgaben entfernte Arbeiten zählen mit; enforce_pub_limit folgt
    if not force and proj.get("reassigned_for") == _reassign_key(proj):
        return
    cur = current_topics(proj)
    if len(cur) < 2:
        proj["reassigned_for"] = _reassign_key(proj)
        return
    log("Themenzuordnung aller Publikationen wird geprüft…")
    manual = proj.get("topic_manual") or {}
    topics = [(tid, t) for tid, t in proj["topics"].items() if t.get("enabled", True) and not t.get("varia")]
    topics_block = "\n".join(
        f"{tid}: {t.get('number', '')} {t['title']}"
        + (f" (Abschnitt {t.get('section_number', '')} {t.get('section_title', '')})" if t.get("section_title") else "")
        for tid, t in topics)
    lines = []
    for p, tid in cur.items():
        rec, d = proj["records"].get(p, {}), proj["pubs"].get(p, {})
        q = proj["topics"][tid].get("ranking", {}).get(p, {}).get("question_key", "")
        summary = " ".join((rec.get("abstract") or d.get("full_text", "")[:900]).split())[:600]
        lines.append(f"- {p}{' [hochgeladen]' if d.get('extra') else ''}{' [vom Nutzer festgelegt]' if p in manual else ''}"
                     f" | {'' if tid == VARIA else tid} | {rec.get('title', p)} | {q} | {summary}")
    res = llm.json(P.reassign_prompt((proj["toc"] or {}).get("chapter_title", ""), topics_block, "\n".join(lines)),
                   REASSIGN_SCHEMA, model=MODEL_MAIN, max_tokens=6000, effort="medium")
    valid = {tid for tid, _ in topics}
    target = {}
    for a in res.get("assignments", []):
        p, tid = a.get("pub_id", ""), a.get("topic_id", "")
        if p not in cur or p in manual:
            continue
        if tid in valid:
            target[p] = (tid, a.get("reason_de", "").strip())
        elif not tid and proj["pubs"].get(p, {}).get("extra"):
            target[p] = (VARIA, a.get("reason_de", "").strip())
    groups, used = [], set()
    for g in res.get("same_question", []):
        ids = [p for p in dict.fromkeys(g.get("pub_ids", [])) if p in cur and p not in used]
        if 2 <= len(ids) <= MAX_GROUP_SIZE:
            used.update(ids)
            groups.append({"pmids": ids, "question_de": g.get("question_de", "").strip()})
    # Gleiche Fragestellung → gleiches Thema: das Thema der ersten (bzw. vom Nutzer festgelegten) Publikation
    for g in groups:
        fixed = next((manual[p] for p in g["pmids"] if p in manual), None)
        tid = fixed or target.get(g["pmids"][0], (cur[g["pmids"][0]], ""))[0]
        for p in g["pmids"]:
            if p not in manual and target.get(p, (cur[p],))[0] != tid:
                target[p] = (tid, f"Gleiche Fragestellung wie „{g['question_de']}“; gemeinsame Würdigung.")
    moved = 0
    for p, (tid, reason) in target.items():
        if tid != cur[p]:
            if tid == VARIA and VARIA not in proj["topics"]:
                proj["pubs"][p].setdefault("home_topic", cur[p])
                proj["pubs"][p]["topic_id"] = ""
                ensure_varia(proj)
            else:
                move_pub(proj, p, tid)
            proj["pubs"][p]["move_reason"] = reason
            moved += 1
    for p, tid in manual.items():
        move_pub(proj, p, tid)
    proj["question_groups"] = groups
    proj["reassigned_for"] = _reassign_key(proj)
    log(f"Themenzuordnung geprüft: {moved} Publikation(en) verschoben, {len(groups)} Gruppe(n) mit gleicher "
        "Fragestellung.")


def rate_unranked(llm: LLM, proj: dict, log) -> None:
    """Zusätzlich hochgeladene Publikationen haben noch keine Qualitätsbewertung aus Schritt 1: Bewertung
    mit demselben Ranking (Studienqualität, Bedeutung der Fragestellung, Impact Factor) nachholen."""
    by_topic: dict[str, list[str]] = {}
    for p, tid in current_topics(proj).items():
        if p not in proj["topics"][tid].get("ranking", {}) and not proj["pubs"].get(p, {}).get("rank_tried"):
            by_topic.setdefault(tid, []).append(p)
    for tid, ids in by_topic.items():
        t = proj["topics"][tid]
        log(f"Qualität von {len(ids)} hochgeladenen Publikation(en) in „{t['title']}“ wird bewertet…")
        recs = []
        for p in ids:
            proj["pubs"].setdefault(p, {})["rank_tried"] = True
            rec, d = proj["records"].get(p, {}), proj["pubs"].get(p, {})
            recs.append({"pmid": p, "title": rec.get("title", p), "journal": rec.get("journal", ""),
                         "year": rec.get("year", ""), "pub_types": rec.get("pub_types") or [],
                         "abstract": rec.get("abstract") or _main_text(d.get("full_text", ""))[:3500]})
        try:
            ranking = recommend.rank(llm, t, "", recs, "des gewählten Zeitraums")
        except AccountError:
            raise
        except Exception as ex:
            _err(proj, f"Qualitätsbewertung {t['title']}", ex)
            continue
        for p, item in ranking.items():
            item["score"] = recommend.score(item)
            item["priority"] = recommend.priority_class(item)
            item["quality"] = recommend.quality(item)
            item["length_score"] = recommend.length_score(item)
            t.setdefault("ranking", {})[p] = item


def _restore_limit_dropped(proj: dict) -> None:
    for e in proj.get("limit_dropped", []):
        p, tid = e["id"], e["topic_id"]
        d = proj["pubs"].get(p, {})
        if d.get("extra"):
            d.pop("limit_dropped", None)
            continue
        t = proj["topics"].get(tid)
        if not t:
            continue
        if e.get("to_reserve"):
            t["reserve"] = [x for x in t.get("reserve", []) if x != p]
        sel = list(t.get("selected", t.get("recommended", [])))
        if p not in sel:
            t["selected"] = sel + [p]
    if proj.get("limit_dropped"):
        bump_selection(proj)
    proj["limit_dropped"] = []


def enforce_pub_limit(proj: dict, log) -> list[dict]:
    """Nach Hochladen und Sortieren: Höchstzahl der Publikationen und Seitenumfang einhalten. Ist eines davon
    überschritten, werden die Studien mit der geringsten Qualität (Qualitätsranking aus Schritt 1) entfernt –
    bevor Kosten für Würdigungen entstehen. Wird neu berechnet, sobald sich Auswahl oder Vorgaben ändern."""
    apply_manual_limits(proj)
    _restore_limit_dropped(proj)
    rules = proj.get("rules") or {}
    max_n, max_pages = rules.get("max_publications") or 0, rules.get("max_pages") or 0
    cur = current_topics(proj)
    if not cur or not (max_n or max_pages):
        return []

    def rank(p):
        it = proj["topics"][cur[p]].get("ranking", {}).get(p, {})
        return (it.get("quality", recommend.quality(it) if it else 0), it.get("length_score", 0))

    def units(ids):
        # Publikationen mit gleicher Fragestellung werden gemeinsam gewürdigt und brauchen nur einmal Platz
        out, seen = [], set()
        for g in proj.get("question_groups", []):
            m = [p for p in g["pmids"] if p in ids and cur.get(p) == cur.get(g["pmids"][0])]
            if len(m) >= 2:
                seen.update(m)
                out.append({"id": "+".join(m), "length_score": max(rank(p)[1] for p in m)})
        return out + [{"id": p, "length_score": rank(p)[1]} for p in ids if p not in seen]

    def too_long(ids):
        if not max_pages:
            return False
        _tiers, overflow = A.plan_tiers(units(ids), rules, _n_sections(proj, {cur[p] for p in ids}))
        return bool(overflow)

    keep = sorted(cur, key=rank, reverse=True)
    dropped = []
    while len(keep) > 1 and ((max_n and len(keep) > max_n) or too_long(keep)):
        p = keep.pop()
        reason = (f"Höchstzahl von {max_n} Publikationen" if max_n and len(keep) >= max_n
                  else f"Seitenumfang (max. {max_pages - 1} Seiten)")
        dropped.append({"id": p, "topic_id": cur[p], "reason": reason,
                        "title": proj["records"].get(p, {}).get("title", p)})
    for e in dropped:
        p, t = e["id"], proj["topics"][e["topic_id"]]
        d = proj["pubs"].setdefault(p, {})
        if d.get("extra"):
            d["limit_dropped"] = True
        else:
            _unselect_everywhere(proj, p)
            if p not in t.get("recommended", []) and p not in t.get("reserve", []):
                t.setdefault("reserve", []).insert(0, p)
                e["to_reserve"] = True
    proj["limit_dropped"] = dropped
    if dropped:
        bump_selection(proj)
        log(f"{len(dropped)} Publikation(en) mit der geringsten Studienqualität wegen der Vorgaben entfernt.")
    return dropped


def check_assignment(llm: LLM, proj: dict, uploads: dict[str, bytes], log,
                     forced_topics: dict[str, str] | None = None) -> None:
    """Pflichtschritt vor dem Schreiben: Uploads zuordnen, Themenzuordnung prüfen, hochgeladene Arbeiten
    bewerten, Publikationszahl und Seitenumfang einhalten. Erst danach wird „Manuskript erstellen“ frei.
    forced_topics: {Dateiname: topic_id} für zusätzliche Publikationen, deren Unterkapitel die Autoren in der
    Auswahlliste festgelegt haben."""
    proj["check_ok"] = False
    if uploads:
        match_uploads(llm, proj, uploads, log)
    for name, tid in (forced_topics or {}).items():
        pm = (proj["uploads"].get(name) or {}).get("pmid")
        if pm and proj["pubs"].get(pm, {}).get("extra") and tid in proj["topics"]:
            proj["pubs"][pm]["topic_id"] = tid
            proj["pubs"][pm]["move_reason"] = "Von den Autoren festgelegt."
            proj.setdefault("topic_manual", {})[pm] = tid
    reassign_topics(llm, proj, log)
    rate_unranked(llm, proj, log)
    enforce_pub_limit(proj, log)
    proj["check_ok"] = True


def apply_selection_package(proj: dict, data: bytes) -> dict:
    """Übernimmt das ZIP-Paket der Auswahlliste (selection_sheet): Auswahl, Reihenfolge und Unterkapitel der
    Autoren gelten als manuell festgelegt; PDFs werden bei ihrer Publikation abgelegt (mit Prüfung), Leitlinien
    dem gewählten Unterkapitel zugeordnet. Zusätzliche Publikationen gibt die Funktion zurück; sie laufen über
    check_assignment (Identifikation). Rückgabe: {"uploads", "forced_topics", "report"}."""
    from .files import extract_text
    from .selection_sheet import read_package
    meta, files = read_package(data)
    # Frühere Kürzungen wegen der Vorgaben nicht wiederherstellen: es zählt die Auswahl der Autoren
    proj["limit_dropped"] = []
    for d in proj["pubs"].values():
        d.pop("limit_dropped", None)
    report = {"selected": 0, "moved": 0, "pdfs": 0, "pdf_warn": [], "extra": 0, "guidelines": 0, "ignored": [],
              "other_project": bool(meta.get("project_id") and meta.get("project_id") != proj.get("id")),
              "edited_by": meta.get("edited_by", "")}
    home = {}
    for tid, t in proj["topics"].items():
        for p in list(t.get("recommended", [])) + list(t.get("reserve", [])):
            home.setdefault(p, tid)
    cur = current_topics(proj)
    rankings = {}
    for t in proj["topics"].values():
        for p, it in (t.get("ranking") or {}).items():
            rankings.setdefault(p, it)
    pkg_topics = [t for t in meta.get("topics", []) if t.get("id") in proj["topics"]]
    report["ignored"] += [t.get("id", "") for t in meta.get("topics", []) if t.get("id") not in proj["topics"]]
    # Auswahl neu aufbauen: nur die Unterkapitel aus dem Paket, alles Angehakte in der Reihenfolge der Autoren
    for t in pkg_topics:
        proj["topics"][t["id"]]["selected"] = []
    uploads, forced = {}, {}
    manual = proj.setdefault("topic_manual", {})
    for t in pkg_topics:
        tid, topic = t["id"], proj["topics"][t["id"]]
        order = []
        for it in t.get("items", []):
            fname = it.get("file_name") or ""
            blob = files.get(it.get("file") or "")
            if it.get("kind") == "extra":
                if not (it.get("checked") and blob):
                    continue
                name = fname or it["file"].rsplit("/", 1)[-1]
                uploads[name], forced[name] = blob, tid
                order.append(f"file:{name}")
                report["extra"] += 1
                continue
            p = it.get("id", "")
            if p not in proj["records"] and p not in proj["pubs"]:
                report["ignored"].append(p)
                continue
            order.append(p)
            d = proj["pubs"].setdefault(p, {})
            if not it.get("checked"):
                if d.get("extra"):
                    d["deselected"] = True
                manual.pop(p, None)
                continue
            report["selected"] += 1
            d.pop("deselected", None)
            src = cur.get(p) or home.get(p)
            if d.get("extra"):
                d["topic_id"] = tid
            else:
                for other in proj["topics"].values():  # jede Arbeit nur in einem Unterkapitel
                    if other is not topic and p in other.get("selected", []):
                        other["selected"].remove(p)
                topic["selected"].append(p)
                if p not in topic.setdefault("ranking", {}) and p in rankings:
                    topic["ranking"][p] = rankings[p]
            if src and src != tid:
                d.setdefault("home_topic", src)
                d["move_reason"] = "Von den Autoren festgelegt."
                report["moved"] += 1
            manual[p] = tid
            if blob:
                name = fname or it["file"].rsplit("/", 1)[-1]
                res = attach_direct_upload(proj, p, name, blob)
                if not res["ok"]:
                    attach_direct_upload(proj, p, name, blob, force=True)
                    d["upload_checked"] = ("von den Autoren bestätigt" if it.get("check") == "bestätigt"
                                           else "passt evtl. nicht, bitte prüfen")
                    if it.get("check") != "bestätigt":
                        report["pdf_warn"].append(proj["records"].get(p, {}).get("title", p))
                report["pdfs"] += 1
        topic["order"] = order
    for g in meta.get("guidelines", []):
        blob = files.get(g.get("file") or "")
        if not blob:
            continue
        name = g.get("file_name") or g["file"].rsplit("/", 1)[-1]
        tid = g.get("topic_id") if g.get("topic_id") in proj["topics"] else ""
        proj["guideline_uploads"][name] = {"topic_id": tid, "text": extract_text(name, blob)}
        report["guidelines"] += 1
    proj["reassigned_for"] = None
    proj["check_ok"] = False
    bump_selection(proj)
    return {"uploads": uploads, "forced_topics": forced, "report": report}


def set_manual_topic(proj: dict, p: str, tid: str) -> None:
    """Zuordnung durch die Nutzerin; die KI-Prüfung ändert sie danach nicht mehr."""
    proj.setdefault("topic_manual", {})[p] = tid
    move_pub(proj, p, tid)
    proj["pubs"][p]["move_reason"] = "Von Ihnen festgelegt."
    proj["reassigned_for"] = _reassign_key(proj)


def assignment_overview(proj: dict) -> list[dict]:
    """Überschriften des Inhaltsverzeichnisses mit den zugeordneten Publikationen (für Anzeige und Word-Liste)."""
    cur = current_topics(proj)
    group_of = {p: g for g in proj.get("question_groups", []) for p in g["pmids"]}
    out, last_sec = [], None
    for tid in topic_order(proj):
        t = proj["topics"][tid]
        if not t.get("enabled", True):
            continue
        sec = (t.get("section_number", ""), t.get("section_title", ""))
        if sec != last_sec and sec[1] and sec[1] != t["title"]:
            out.append({"kind": "section", "text": f"{sec[0]} {sec[1]}".strip()})
        last_sec = sec
        pubs = []
        for p in (p for p, x in cur.items() if x == tid):
            rec, d = proj["records"].get(p, {}), proj["pubs"].get(p, {})
            home = d.get("home_topic")
            g = group_of.get(p)
            pubs.append({"id": p, "title": rec.get("title", p), "journal": rec.get("journal", ""),
                         "year": rec.get("year", ""), "uploaded": bool(d.get("extra")),
                         "moved_from": proj["topics"][home]["title"] if home and home != tid and home in proj["topics"] else "",
                         "reason": d.get("move_reason", "") if home and home != tid else "",
                         "joint": g["question_de"] if g and all(cur.get(m) == tid for m in g["pmids"]) else ""})
        out.append({"kind": "topic", "id": tid, "text": f"{t.get('number', '')} {t['title']}".strip(), "pubs": pubs})
    return out


def assignment_docx(proj: dict) -> bytes:
    from io import BytesIO
    from docx import Document
    doc = Document()
    doc.add_heading(f"Themenzuordnung – {(proj['toc'] or {}).get('chapter_title', '')}", level=1)
    for row in assignment_overview(proj):
        if row["kind"] == "section":
            doc.add_heading(row["text"], level=2)
            continue
        doc.add_heading(row["text"], level=3)
        if not row["pubs"]:
            doc.add_paragraph().add_run("Keine Publikation (entfällt im Manuskript).").italic = True
        for x in row["pubs"]:
            para = doc.add_paragraph(style="List Bullet")
            para.add_run(f"{x['title']} — {x['journal']} {x['year']}".strip())
            notes = (["hochgeladen"] if x["uploaded"] else []) + \
                ([f"verschoben aus „{x['moved_from']}“: {x['reason']}".rstrip(": ")] if x["moved_from"] else []) + \
                ([f"gemeinsame Würdigung: {x['joint']}"] if x["joint"] else [])
            if notes:
                para.add_run(" (" + "; ".join(notes) + ")").italic = True
    if proj.get("limit_dropped"):
        doc.add_heading("Wegen der Vorgaben nicht aufgenommen", level=2)
        for e in proj["limit_dropped"]:
            doc.add_paragraph(f"{e['title']} ({proj['topics'].get(e['topic_id'], {}).get('title', '')}; {e['reason']})",
                              style="List Bullet")
    bio = BytesIO()
    doc.save(bio)
    return bio.getvalue()


# ======================================================================
# SCHRITT 2: Automatische Erstellung
# ======================================================================

def _pub_list(proj: dict) -> list[tuple[str, str]]:
    """[(pmid, topic_id)] in Manuskript-Reihenfolge: Themen wie im Inhaltsverzeichnis, darin nach Score."""
    out, seen = [], set()
    for tid in topic_order(proj):
        t = proj["topics"][tid]
        if not t.get("enabled", True):
            continue
        # Eine Publikation steht nur einmal im Manuskript (z.B. nach Verschieben in ein anderes Thema)
        ids = [p for p in t.get("selected", t.get("recommended", [])) if p not in seen]
        ids += [p for p, d in proj["pubs"].items() if d.get("extra") and d.get("topic_id") == tid and p not in ids
                and not d.get("limit_dropped") and not d.get("deselected")]
        rk = t.get("ranking", {})
        ids.sort(key=lambda p: -rk.get(p, {}).get("score", 0))
        if t.get("order"):  # von den Autoren festgelegte Reihenfolge (Auswahlliste); Unbekanntes ans Ende
            by_file = {f"file:{n}": u.get("pmid") for n, u in (proj.get("uploads") or {}).items() if u.get("pmid")}
            pos = {by_file.get(k, k): i for i, k in enumerate(t["order"])}
            ids.sort(key=lambda p: pos.get(p, len(pos)))
        seen.update(ids)
        out.extend((p, tid) for p in ids)
    return out


def _guideline_docs(proj: dict, tid: str, query: str, per_doc: int = 6000, total: int = 14000) -> list:
    docs = []
    t = proj["topics"][tid]
    # Von den Autoren gezielt hochgeladene Leitlinien zuerst, damit sie nicht an der Gesamtgrenze scheitern
    for name, g in proj.get("guideline_uploads", {}).items():
        if g.get("topic_id") == tid and g.get("text"):
            docs.append((f"Vom Autor bereitgestellt: {name}", G.relevant_excerpt(g["text"], query, per_doc)))
    for g in t.get("guidelines", []):
        if g.get("text"):
            label = f"Leitlinie: {g.get('title', '')} ({g.get('society', '')}, {g.get('year', '')})"
            docs.append((label, G.relevant_excerpt(g["text"], query, per_doc)))
    for name, g in proj.get("guideline_uploads", {}).items():
        if g.get("topic_id") == "" and g.get("text"):
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


def _fetch_fulltexts(proj: dict, pmids: list[str]) -> None:
    """Volltexte der Publikationen, soweit nicht hochgeladen: frei verfügbare (Open Access) selbst laden."""
    for pmid in pmids:
        d = proj["pubs"].setdefault(pmid, {})
        rec = proj["records"].get(pmid, {})
        if not d.get("full_text") and not d.get("oa_tried"):
            d["oa_tried"] = True
            pdf = fetch_oa_pdf(rec.get("doi", ""), proj["email"])
            if pdf:
                text = extract_pdf_text(pdf)
                if looks_like_real_fulltext(text, rec.get("abstract", "")):
                    d["full_text"], d["fulltext_source"] = text, "Open Access"
        # Abbildungen werden nicht mehr automatisch ausgewählt (2026-10-11): PDF nach der Textextraktion
        # nicht mehr im Speicher halten
        d.pop("pdf", None)
        d.pop("images", None)


def _is_lean(proj: dict, p: str) -> bool:
    """Die Publikation hat (nur) eine schlanke Würdigung – genügt für eine einzeln stehende Kurznachricht."""
    return (proj["pubs"].get(p) or {}).get("appraisal_kind") == A.LEAN_KIND


def _appraisal_inputs(proj: dict, p: str, tid: str, with_docs: bool = True) -> tuple[dict, list]:
    rec = proj["records"].get(p, {})
    docs = []
    if with_docs:
        query = f"{proj['topics'][tid]['title']} {rec.get('title', '')} {rec.get('abstract', '')[:600]}"
        docs = _guideline_docs(proj, tid, query)
    return _entry(rec, proj["pubs"][p]), docs


# Felder, die auf der Würdigung aufbauen und neu entstehen müssen, wenn sie ersetzt wird
_APPRAISAL_DERIVED = ("body_en", "literature", "short_en", "short_de", "styled_de", "style_ok", "short_v",
                      "short_compact", "short_tier")


def _appraise(llm: LLM, proj: dict, items: list[tuple[str, str]], lean: set[str], log, batch: bool,
              replace: bool = False) -> None:
    """Kritische Würdigungen für items = [(PMID, Thema)] erstellen und sofort im Projekt ablegen.

    lean: Publikationen, die nur eine schlanke Würdigung erhalten (geplante Kurznachrichten). batch: als
    Sammelauftrag zum halben Preis statt sofort; die Aufträge sind in beiden Fällen dieselben
    (appraisal.appraisal_job / lean_appraisal_job). replace: eine vorhandene schlanke Würdigung durch eine
    vollständige ersetzen; schlägt das fehl, bleibt die schlanke Fassung bestehen.

    Im Sammelauftrag liegt die Antwort der KI zunächst unbearbeitet im Projekt (appraisal_raw). Die
    Nachbearbeitung der Literaturziffern (bei Bedarf ein kurzer direkter KI-Aufruf) folgt danach; hält der Lauf
    dazwischen an, holt der nächste Start sie nach, ohne die Würdigung erneut zu bezahlen.

    Auch der direkte Ablauf (batch=False) übernimmt zuerst, was für dieses Projekt aus einem Sammelauftrag
    schon bezahlt ist – unbearbeitete Antworten im Projekt und Ergebnisse, die noch bei Anthropic liegen (ein
    eigener, noch laufender Sammelauftrag wird abgewartet). Erst was dann fehlt, wird direkt erstellt. So kostet
    der Wechsel vom Sparmodus zum direkten Ablauf nichts doppelt."""
    def title(p: str) -> str:
        return proj["records"].get(p, {}).get("title", p)[:60]

    def store(p: str, res, kind: str) -> None:
        d = proj["pubs"][p]
        d.pop("appraisal_raw", None)
        if isinstance(res, Exception):
            _err(proj, f"Würdigung {title(p)}" + (" (es bleibt bei der schlanken Fassung)" if replace else ""), res)
            return
        if replace:
            if not res:
                return  # leere Antwort: die schlanke Fassung bleibt
            for k in _APPRAISAL_DERIVED:
                d.pop(k, None)
        d["appraisal_en"] = res
        if kind == A.LEAN_KIND:
            d["appraisal_kind"] = kind
        else:
            d.pop("appraisal_kind", None)

    def waiting() -> list[tuple[str, str]]:  # noch ohne (vollständige) Würdigung
        return [(p, t) for p, t in items
                if (_is_lean(proj, p) if replace else not proj["pubs"][p].get("appraisal_en"))]

    def finish_raw() -> None:
        ready = [(p, p) for p, _ in items if proj["pubs"][p].get("appraisal_raw")]

        def work(p):
            raw = proj["pubs"][p]["appraisal_raw"]
            if raw.get("kind") == A.LEAN_KIND:
                return A.finish_lean_appraisal(raw.get("text", ""))
            return A.finish_appraisal(llm, raw.get("text", ""), "Englisch", strict=True)

        for p, res in _parallel(ready, work, log, "Nachbearbeitung der Würdigungen").items():
            store(p, res, (proj["pubs"][p].get("appraisal_raw") or {}).get("kind") or "")

    finish_raw()  # Antworten eines früheren, angehaltenen Sammelauftrags
    if hasattr(llm, "run_batch"):
        jobs, owner = [], {}
        for p, tid in waiting():
            entry, docs = _appraisal_inputs(proj, p, tid, with_docs=p not in lean)
            # Zur Kennung des Auftrags gehört, welche Unterlagen vorlagen (Volltext ja/nein, Zahl der
            # Leitlinienauszüge). Kommt später ein Volltext oder eine Leitlinie hinzu, wird ein älteres, noch bei
            # Anthropic liegendes Ergebnis nicht stillschweigend übernommen: Die Würdigung entsteht dann mit den
            # besseren Unterlagen neu. Der Wortlaut der Vorgabe gehört nicht zur Kennung, damit bezahlte Ergebnisse
            # eine Programmänderung überstehen.
            have = f"f{int(bool(entry.get('full_text')))}g{len(docs)}"
            job = (A.lean_appraisal_job(entry, "Englisch", key=f"wl|{p}|{have}") if p in lean
                   else A.appraisal_job(entry, docs, "Englisch", key=f"w|{p}|{have}"))
            jobs.append(job)
            owner[job.key] = (p, A.LEAN_KIND if p in lean else "")
        if jobs:
            state = proj.setdefault("batch", {}).setdefault("w", {})
            try:
                # batch=False: nur abholen (submit=False), nichts einreichen
                for key, res in llm.run_batch(jobs, prefix=f"{proj['id']}-w", state=state, log=log, submit=batch):
                    p, kind = owner[key]
                    if not isinstance(res, Exception):
                        # sofort im Projekt: bezahlt ist bezahlt
                        proj["pubs"][p]["appraisal_raw"] = {"text": res, "kind": kind}
                    elif batch:
                        store(p, res, kind)
                    # im direkten Ablauf wird ein im Sammelauftrag gescheiterter Auftrag gleich direkt nachgeholt
            finally:
                if not batch:
                    _tidy_batch_state(proj, "w")
            finish_raw()
    if batch:
        return

    def work(item):
        p, tid = item
        entry, docs = _appraisal_inputs(proj, p, tid, with_docs=p not in lean)
        if p in lean:
            return A.generate_lean_appraisal(llm, entry, "Englisch")
        return A.generate_appraisal(llm, entry, docs, "Englisch")

    for p, res in _parallel([(p, (p, t)) for p, t in waiting()], work, log, "Kritische Würdigung").items():
        store(p, res, A.LEAN_KIND if p in lean else "")


def _set_body(proj: dict, p: str) -> None:
    """Arbeitsfassung der Würdigung (body_en) und lokale Literaturliste (Nr. 1 = die Publikation selbst)."""
    d = proj["pubs"][p]
    if "body_en" in d:
        return
    main = pubmed.format_reference(proj["records"].get(p, {}))
    if _is_lean(proj, p):
        # Eine schlanke Würdigung hat weder Literaturziffern noch Literaturliste (die Suche danach würde eine
        # nummerierte Aufzählung im Text für eine Literaturliste halten)
        body, entries = d["appraisal_en"], []
    else:
        body, entries = extract_and_renumber_citations_global(d["appraisal_en"], 2)
    body, lit = dedupe_local_literature(body, [main] + entries)
    d["body_en"], d["literature"] = strip_appraisal_preamble(body), lit


def _upgrade_lean(llm: LLM, proj: dict, need: list[tuple[str, str]], log, batch: bool) -> None:
    """Schlank gewürdigte Publikationen vollständig würdigen (sie stehen nun in einer gemeinsamen Würdigung,
    sind länger als eine Kurznachricht geplant, oder die schlanke Würdigung wurde abgeschaltet)."""
    need = [(p, t) for p, t in need if _is_lean(proj, p)]
    if not need:
        return
    log(f"{len(need)} bisher schlank gewürdigte Publikation(en) werden vollständig gewürdigt (sie stehen nicht "
        "mehr als einzelne Kurznachricht im Manuskript)…")
    _appraise(llm, proj, need, set(), log, batch, replace=True)
    for p, _ in need:
        _set_body(proj, p)


def _singles_by_topic(proj: dict, pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    by_topic: dict[str, list[str]] = {}
    for p, t in pairs:
        if not (proj["pubs"].get(p) or {}).get("group"):
            by_topic.setdefault(t, []).append(p)
    return by_topic


def _ensure_groups(llm: LLM, proj: dict, by_topic: dict[str, list[str]], log) -> None:
    """Gemeinsame Fragestellungen je Thema bestimmen (ein KI-Aufruf je Thema mit mindestens zwei Publikationen;
    das Ergebnis gilt, solange dieselben Publikationen im Thema stehen)."""
    for tid, pmids in by_topic.items():
        t = proj["topics"][tid]
        if len(pmids) < 2:
            continue
        key = [f"v{GROUP_VERSION}"] + sorted(pmids)
        if t.get("groups_for") != key:
            log(f"Gemeinsame Fragestellungen in „{t['title']}“ werden geprüft…")
            try:
                t["groups"], t["groups_for"] = find_groups(llm, proj, tid, pmids), key
            except AccountError:
                raise
            except Exception as ex:
                _err(proj, f"Gruppierung {t['title']}", ex)


def _valid_groups(proj: dict, by_topic: dict[str, list[str]]) -> dict[str, tuple[str, dict]]:
    """{Kennung der gemeinsamen Würdigung: (Thema, Gruppe)} für alle Gruppen, deren Publikationen vollständig
    im Thema stehen."""
    out = {}
    for tid, pmids in by_topic.items():
        for g in proj["topics"][tid].get("groups", []):
            if all(p in pmids for p in g["pmids"]):
                out["G-" + "-".join(g["pmids"])] = (tid, g)
    return out


def _preview_tiers(llm: LLM, proj: dict, pairs: list[tuple[str, str]], log, joint: bool,
                   options: dict) -> tuple[dict[str, str], set[str]]:
    """Längenstufen vorab bestimmen – nach denselben Regeln, mit denen run_manuscript sie nach den Würdigungen
    plant (gemeinsame Würdigungen zählen als eine Einheit; eine bestehende Planung bleibt, solange sich
    Publikationen und Vorgaben nicht ändern). Grundlage für die Entscheidung, welche Publikationen nur eine
    schlanke Würdigung brauchen. Rückgabe: (Stufe je Publikation bzw. gemeinsamer Würdigung, Publikationen in
    einer gemeinsamen Würdigung)."""
    member_of: dict[str, str] = {}
    if joint:
        by_topic = _singles_by_topic(proj, pairs)
        _ensure_groups(llm, proj, by_topic, log)
        for gid, (_tid, g) in _valid_groups(proj, by_topic).items():
            for m in g["pmids"]:
                member_of[m] = gid
    units, seen = [], set()
    for p, t in pairs:
        u = member_of.get(p, p)
        if u in seen:
            continue
        seen.add(u)
        rk = proj["topics"][t].get("ranking", {})
        members = [m for m, g in member_of.items() if g == u] if u != p else [p]
        best = max((rk.get(m, {}) for m in members), key=lambda it: it.get("length_score", 0), default={})
        units.append(({"id": u, "length_score": best.get("length_score", 50)}, t))
    apply_manual_limits(proj)
    rules = proj.get("rules") or {}
    limit_key = [rules.get("max_pages"), rules.get("max_references"), rules.get("tier_counts")]
    current = proj.get("tiers") or {}
    if (not current or options.get("replan_tiers") or any(u["id"] not in current for u, _ in units)
            or proj.get("tiers_for_limits") != limit_key):
        tiers, overflow = A.plan_tiers([u for u, _ in units], rules, _n_sections(proj, {t for _, t in units}))
        for u in overflow:
            tiers[u] = A.SHORTEST_TIER
    else:
        tiers = dict(current)
    return tiers, set(member_of)


def run_manuscript(llm: LLM, proj: dict, log, options: dict) -> None:
    email = proj["email"]
    # Ein älteres Manuskript (z.B. aus einem geladenen Zwischenstand) darf nach einem Abbruch nicht als
    # Ergebnis dieses Laufs heruntergeladen werden.
    for k in ("manuskript_DE", "manuskript_EN", "rohfassung_DE", "rohfassung_EN", "pruefprotokoll", "befunde",
              "created", "version"):
        (proj.get("outputs") or {}).pop(k, None)
    apply_manual_limits(proj)
    ensure_varia(proj)
    try:
        reassign_topics(llm, proj, log)  # ist nach check_assignment bereits erledigt (Cache)
    except AccountError:
        raise
    except Exception as ex:
        _err(proj, "Themenzuordnung prüfen", ex)
    rate_unranked(llm, proj, log)
    enforce_pub_limit(proj, log)
    pubs = _pub_list(proj)
    if options.get("trial"):
        # Probelauf: gewähltes (sonst erstes) Thema mit Publikationen, höchstens 2 Arbeiten – kostet nur
        # einen Bruchteil
        wanted = options.get("trial_topic")
        first = wanted if any(t == wanted for _, t in pubs) else next((t for _, t in pubs), None)
        pubs = [(p, t) for p, t in pubs if t == first][:2]
    log(f"{len(pubs)} Publikationen in {len({t for _, t in pubs})} Themen werden verarbeitet.")

    # 1. Volltexte (Upload oder Open Access)
    _fetch_fulltexts(proj, [p for p, _ in pubs])

    # 2. Kritische Würdigung (englisch, wie in MyLuo als Arbeitsfassung)
    joint = bool(options.get("joint", True))
    batch = bool(options.get("batch")) and hasattr(llm, "run_batch")  # Sparmodus: Sammelauftrag zum halben Preis
    lean_on = bool(options.get("lean_news"))
    todo = [(p, t) for p, t in pubs if not proj["pubs"][p].get("appraisal_en")]
    lean: set[str] = set()
    if lean_on and todo:
        # Schlanke Würdigung nur für Publikationen, die als Kurznachricht geplant sind und nicht in einer
        # gemeinsamen Würdigung stehen; dafür werden die Längen vorab nach denselben Regeln geplant
        tiers0, grouped0 = _preview_tiers(llm, proj, pubs, log, joint, options)
        lean = {p for p, _ in todo if tiers0.get(p) == "Kurznachricht" and p not in grouped0}
    if todo:
        log("Kritische Würdigungen werden erstellt "
            + ("(Sparmodus: als Sammelauftrag, das Ergebnis kommt meist innerhalb einer Stunde)…" if batch
               else "(mit Websuche, das dauert mehrere Minuten pro Publikation)…")
            + (f" Davon {len(lean)} schlank (geplante Kurznachrichten)." if lean else ""))
    _appraise(llm, proj, todo, lean, log, batch)

    ready = [(p, t) for p, t in pubs if proj["pubs"][p].get("appraisal_en")]

    # 3. Lokale Literaturliste (Nr. 1 = Publikation selbst) und Längenstufen
    for p, _ in ready:
        _set_body(proj, p)
    # 3a. Eine schlanke Würdigung genügt nur für eine einzeln stehende Kurznachricht. Steht die Publikation
    # jetzt in einer gemeinsamen Würdigung (oder ist die schlanke Würdigung abgeschaltet), wird sie vollständig
    # gewürdigt, bevor es weitergeht.
    need = [(p, t) for p, t in ready if _is_lean(proj, p)]
    if need and lean_on:
        grouped: set[str] = set()
        if joint:
            by_topic = _singles_by_topic(proj, ready)
            _ensure_groups(llm, proj, by_topic, log)
            grouped = {m for _tid, g in _valid_groups(proj, by_topic).values() for m in g["pmids"]}
        # … ebenso, wenn die vollständige Würdigung aus einem früheren, angehaltenen Lauf schon bezahlt vorliegt
        need = [(p, t) for p, t in need
                if p in grouped or (proj["pubs"][p].get("appraisal_raw") or {}).get("kind") == ""]
    _upgrade_lean(llm, proj, need, log, batch)
    # 3b. Gemeinsame Würdigung für Publikationen mit nahezu gleicher Fragestellung (wie MyLuo, Schritt 4B)
    if joint:
        ready = apply_groups(llm, proj, ready, log)

    apply_manual_limits(proj)
    prev_tiers = dict(proj.get("tiers") or {})
    limit_key = [(proj.get("rules") or {}).get("max_pages"), (proj.get("rules") or {}).get("max_references"),
                 (proj.get("rules") or {}).get("tier_counts")]
    if (not proj.get("tiers") or options.get("replan_tiers") or any(p not in proj["tiers"] for p, _ in ready)
            or proj.get("tiers_for_limits") != limit_key):
        proj["tiers_for_limits"] = limit_key
        # Selbst ausgewählte Arbeiten werden nicht verworfen: im Notfall alle auf ½ Seite, Hinweis im Protokoll
        tiers, overflow = A.plan_tiers(_length_info(proj, ready), proj.get("rules") or {},
                                       _n_sections(proj, {t for _, t in ready}))
        for p in overflow:
            tiers[p] = A.SHORTEST_TIER
        proj["tiers"] = tiers
    for p, _ in ready:
        proj["tiers"].setdefault(p, A.SHORTEST_TIER)
    # 3c. Schlank gewürdigt, aber nun doch länger als eine Kurznachricht geplant (z.B. nach einer geänderten
    # Auswahl oder Verteilung): vollständig würdigen
    _upgrade_lean(llm, proj, [(p, t) for p, t in ready
                              if _is_lean(proj, p) and proj["tiers"].get(p) != "Kurznachricht"], log, batch)

    # 4. Kürzung (englisch). Auf Wunsch werden ältere Kürzungen (vor SHORT_VERSION) neu erstellt; dann
    # auch Übersetzung und Stilfassung, die darauf aufbauen.
    compact = {"stats": bool(options.get("compact_stats")), "comment": bool(options.get("compact_comment"))}
    for p, _ in ready:
        d = proj["pubs"][p]
        old = d.get("short_en") and options.get("reshorten") and d.get("short_v", 1) < SHORT_VERSION
        # Häkchen „Statistik/Kommentar kompakt“ geändert: diese Würdigung neu kürzen
        changed = d.get("short_en") and (d.get("short_compact") or {"stats": False, "comment": False}) != compact
        # geplante Länge geändert (z.B. neue Verteilung lang/mittel/kurz): neu kürzen
        retier = d.get("short_en") and ((d.get("short_tier") or prev_tiers.get(p)) or proj["tiers"].get(p)) \
            != proj["tiers"].get(p)
        # „1 Seite (Lang)“ behält den Namen, ist aber seit 2026-11-03 kürzer: ältere Fassungen neu kürzen
        retier = retier or (d.get("short_en") and proj["tiers"].get(p) == "1 Seite (Lang)" and d.get("short_v", 1) < 11)
        if old or changed or retier:
            for k in ("short_en", "short_de", "styled_de", "style_ok"):
                d.pop(k, None)
    todo = [(p, p) for p, _ in ready if not proj["pubs"][p].get("short_en")]

    # Literaturbudget: Hauptquellen + State of the Art (≈3 je Abschnitt) zuerst, der Rest verteilt sich auf
    # die externen Verweise der Würdigungen (mindestens einer je Würdigung)
    max_refs = (proj.get("rules") or {}).get("max_references") or 0
    ext_budget = None
    if max_refs:
        n_main = sum(len(proj["pubs"][p].get("group") or [p]) for p, _ in ready)
        n_sota = _n_sections(proj, {t for _, t in ready}) * 3 if SOTA_ENABLED else 0
        ext_budget = max(1, (max_refs - n_main - n_sota) // max(1, len(ready)))

    def shorten(p):
        d, rec = proj["pubs"][p], proj["records"].get(p, {})
        if d.get("group"):
            return A.shorten_joint(llm, d["body_en"], proj["tiers"][p], "Englisch", max_ext=ext_budget,
                                   compact=compact)
        intro = A.split_intro(d["full_text"])[0] if d.get("full_text") else ""
        return A.shorten(llm, d["body_en"], proj["tiers"][p], "Englisch", rec.get("abstract", ""),
                         max_ext=ext_budget, compact=compact, intro=intro if len(intro) >= 300 else "")

    log("Würdigungen werden auf die geplanten Längen gekürzt…")
    for p, res in _parallel(todo, shorten, log, "Kürzung").items():
        if isinstance(res, Exception):
            _err(proj, f"Kürzung {p}", res)
            proj["pubs"][p]["short_en"] = proj["pubs"][p]["body_en"]  # ungekürzt weiterverwenden
        else:
            proj["pubs"][p]["short_en"], proj["pubs"][p]["short_v"] = res, SHORT_VERSION
            proj["pubs"][p]["short_compact"] = dict(compact)
            proj["pubs"][p]["short_tier"] = proj["tiers"][p]

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

    # 6. State of the Art: einer je Hauptabschnitt, vor dem Thema mit der hochwertigsten Publikation
    # (bei Gleichstand: mit den meisten Publikationen)
    topic_ids = [t for t in topic_order(proj) if any(tt == t for _, tt in ready)]
    counts = {t: sum(len(proj["pubs"][p].get("group") or [p]) for p, tt in ready if tt == t) for t in topic_ids}
    best_q = {t: max((recommend.quality(_rank_item(proj, p, t)) for p, tt in ready if tt == t),
                     default=0.0) for t in topic_ids}
    sota_ids = [t for t in A.sota_topics(topic_ids, proj["topics"], counts, best_q)
                if not proj["topics"][t].get("varia")]
    proj["sota_topics"] = sota_ids

    def first_pub(tid):
        """Die erste Publikation des Themas mit Volltext (sie folgt direkt auf den State of the Art);
        bei einer gemeinsamen Würdigung die erste beteiligte Publikation mit Volltext."""
        for p, tt in ready:
            if tt != tid:
                continue
            for m in proj["pubs"][p].get("group") or [p]:
                if proj["pubs"].get(m, {}).get("full_text"):
                    return m
        return None

    # Neu erstellen, wenn noch keiner da ist oder ein älterer (per Websuche) durch einen aus der
    # Einleitung der folgenden Publikation ersetzt werden kann
    todo = [(t, t) for t in sota_ids if not proj["topics"][t].get("sota_en")
            or (proj["topics"][t]["sota_en"].get("source") != A.SOTA_SOURCE and first_pub(t))]

    def sota(tid):
        title = proj["topics"][tid]["title"]
        en = None
        if first_pub(tid):
            pubs = []  # alle Publikationen des Themas mit Volltext, in Manuskript-Reihenfolge
            for q, tt in ready:
                if tt != tid:
                    continue
                for m in proj["pubs"][q].get("group") or [q]:
                    if proj["pubs"].get(m, {}).get("full_text") and m not in [x for x, _ in pubs]:
                        pubs.append((m, proj["pubs"][m]["full_text"]))
            en = A.generate_sota_from_intro(
                llm, title, [(proj["records"].get(m, {}).get("title", ""), ft) for m, ft in pubs],
                _guideline_docs(proj, tid, title, per_doc=4000, total=8000))
        if en is None:
            en = A.generate_sota(llm, title, "Englisch", _guideline_docs(proj, tid, title, per_doc=5000, total=12000))
        return {"sota_en": en, "sota_de": A.translate_sota(llm, en)}

    if SOTA_ENABLED and options.get("sota", True):
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

    # 7a. Fazit-Anfänge abwechslungsreich (nicht überall "Das Ergebnis ist neu.")
    try:
        vary_fazit_openings(llm, proj, ready, log)
    except AccountError:
        raise
    except Exception as ex:
        _err(proj, "Fazit-Formulierungen", ex)

    # 7b. Inhaltliche Rückbezüge auf frühere eigene Manuskripte (höchstens OWN_REFS_MAX, nur wo sinnvoll)
    if proj.get("style") and options.get("own_refs", True):
        key = sorted(p for p, _ in ready)
        if proj.get("own_refs_for") != key:
            log("Rückbezüge auf frühere Manuskripte werden gesucht…")
            try:
                proj["own_refs"] = find_own_refs(llm, proj, ready)
                proj["own_refs_for"] = key
            except AccountError:
                raise
            except Exception as ex:
                _err(proj, "Rückbezüge auf frühere Manuskripte", ex)

    # 7c. Gemeinsamer Kommentar für 2–4 kurz gewürdigte Publikationen eines Themas (Vorbild Decker)
    if options.get("topic_comment", True):
        try:
            topic_comments(llm, proj, ready, log)
        except AccountError:
            raise
        except Exception as ex:
            _err(proj, "Gemeinsame Kommentare", ex)

    # 7d. Gelegentliche Überleitungen zwischen den Publikationen eines Themas (Vorbild Decker)
    if options.get("transitions", True):
        try:
            find_transitions(llm, proj, ready, log)
        except AccountError:
            raise
        except Exception as ex:
            _err(proj, "Überleitungen", ex)

    # 9. Manuskripte zusammensetzen, Literatur zusammenführen, Formatvorlage, formale Prüfung
    log("Gesamtmanuskript wird zusammengesetzt und formal geprüft…")
    rules = proj.get("rules") or {}
    title = (proj["toc"] or {}).get("chapter_title") or "Gesamtmanuskript"
    template = (proj.get("template") or {}).get("bytes")
    # Merkfunktion (2026-11-13): Beim erneuten Erstellen gehen nur neue oder geänderte Textstellen und
    # Literatureinträge an die KI; die Prüfergebnisse der übrigen stehen im Projekt.
    build_cache = proj.setdefault("build_cache", {})
    formatter = make_formatter(llm, rules, proj["records"], email, cache=build_cache.setdefault("refs", {}))
    gliederung = outline(proj, topic_ids)
    try:
        de = headings_de(llm, proj, topic_ids)
    except AccountError:
        raise
    except Exception as ex:
        _err(proj, "Übersetzung der Überschriften", ex)
        de = {}
    kw = {}
    for tid in (SAFETY, VARIA):
        if tid in topic_ids:
            ids = [p for p, tt in ready if tt == tid]
            try:
                kw[tid] = keyword_headings(llm, proj, tid, ids)
            except AccountError:
                raise
            except Exception as ex:
                _err(proj, "Schlagwort-Überschriften", ex)
                kw[tid] = [("Weitere Publikationen", "Further publications", ids)]
    for lang in ("Deutsch", "Englisch"):
        blocks = []
        key_refs = []  # Hauptquellen der ausführlich gewürdigten Publikationen: fett im Literaturverzeichnis
        lk = "de" if lang == "Deutsch" else "en"
        tr = (lambda x: de.get(x, x)) if lang == "Deutsch" else (lambda x: x)
        for tid in topic_ids:
            t = proj["topics"][tid]
            sota_block = (t.get("sota_de" if lang == "Deutsch" else "sota_en")
                          if SOTA_ENABLED and tid in sota_ids and options.get("sota", True) else None)
            g = dict(gliederung.get(tid, {}))
            for k in ("section", "subsection"):
                if g.get(k):
                    g[k] = (g[k][0], tr(g[k][1]))
            block = {"topic": tr(t["title"]), "sota": sota_block, "pubs": [], **g,
                     "comment": (t.get("topic_comment") or {}).get(lk) if options.get("topic_comment", True) else None}
            transitions = (t.get("transitions") or {}).get("items", {}) if options.get("transitions", True) else {}
            sub_blocks = None
            if tid in kw:  # „Neue Themen“ / „Varia“: Hauptabschnitt mit je einer Schlagwort-Überschrift
                sec_num = (g.get("section") or (g.get("number") or "", ""))[0]
                sec_title = ("Neue Themen" if lang == "Deutsch" else "New topics") if tid == SAFETY else "Varia"
                sub_blocks = []
                for k, (h_de, h_en, ids) in enumerate(kw[tid], start=1):
                    sub_blocks.append({"topic": h_de if lang == "Deutsch" else h_en, "sota": None, "pubs": [],
                                       "comment": None, "section": (sec_num, sec_title) if k == 1 else None,
                                       "subsection": None, "number": f"{sec_num}.{k}" if sec_num else None,
                                       "level": 2, "_ids": ids})
                transitions = {}
            for p, tt in ready:
                if tt != tid:
                    continue
                if sub_blocks is not None:
                    block = next(b for b in sub_blocks if p in b["_ids"])
                d, rec = proj["pubs"][p], proj["records"].get(p, {})
                if lang == "Deutsch":
                    text = d.get("styled_de") or d.get("short_de") or d["short_en"]
                    abstract = d.get("abstract_de")
                else:
                    text = d["short_en"]
                    abstract = rec.get("abstract") if proj["tiers"].get(p) == "2 Seiten" else None
                own = (proj.get("own_refs") or {}).get(p) if options.get("own_refs", True) else None
                if own:
                    text = insert_own_ref(text, own["de" if lang == "Deutsch" else "en"])
                members = d.get("group") or []
                text, lit = reconcile_local_citations(text, d["literature"], protected_count=max(1, len(members)))
                if members:
                    rec = dict(rec, title=d["title_de" if lang == "Deutsch" else "title_en"])
                if proj["tiers"].get(p) in LONG_TIERS:
                    key_refs.extend(lit[: max(1, len(members))])
                lines = [_strip_stray_label(l) for l in text.split("\n") if l.strip()]
                if (transitions.get(p) or {}).get(lk):
                    lines = prepend_transition(lines, transitions[p][lk])
                block["pubs"].append({"title": rec.get("title", p), "rec": rec, "abstract": abstract,
                                      "recs": [proj["records"].get(m, {}) for m in members],
                                      "lines": lines,
                                      "literature": lit,
                                      "kurz": proj["tiers"].get(p) == "Kurznachricht",
                                      "figure": None})
            if sub_blocks is not None:
                for b in sub_blocks:
                    b.pop("_ids", None)
                    blocks.append(b)
            else:
                blocks.append(block)
        raw = build_docx(tr(title), blocks, lang)
        key = "DE" if lang == "Deutsch" else "EN"
        try:
            befunde, final = run_formal_check(raw, llm if lang == "Deutsch" else None, template, rules, formatter,
                                              bold_refs=key_refs, cache=build_cache)
        except Exception as ex:
            _err(proj, f"Formale Prüfung {key}", ex)
            befunde, final = [], raw
        proj["outputs"][f"manuskript_{key}"] = final
        proj["outputs"]["created"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
        proj["outputs"]["version"] = APP_VERSION
        proj["outputs"][f"rohfassung_{key}"] = raw
        if lang == "Deutsch":
            befunde = _pipeline_findings(proj, ready) + befunde
            proj["outputs"]["befunde"] = befunde
            proj["outputs"]["pruefprotokoll"] = build_pruefprotokoll_docx(befunde)
    log("Fertig.")


_STRAY_LABEL = re.compile(r"^\s*(\*\*)?(State of the Art|Stand der Forschung|Hintergrund|Background)\s*:\s*(\*\*)?\s*",
                          re.I)


def _strip_stray_label(line: str) -> str:
    """Ein von der KI erfundenes Label wie „State of the art:“ mitten in einer Würdigung entfernen; der
    State of the Art steht nur einmal je Hauptabschnitt vor dem Thema (Wunsch der Autorin, 2026-10-13)."""
    return _STRAY_LABEL.sub("", line)


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
    n_lean = sum(1 for p, _ in ready if _is_lean(proj, p))
    if n_lean:
        out.append({"punkt": "Schlanke Würdigung für Kurznachrichten", "status": "✅",
                    "begruendung": f"{n_lean} Kurznachricht(en) beruhen auf einer schlanken Würdigung: Methodik, "
                    "Validität und Schlussfolgerungen der Autoren wurden geprüft; Websuche, Leitlinienbezug und "
                    "Kommentar entfielen, weil eine Kurznachricht sie nicht enthält."})
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
# Kostenaufstellung je Arbeitsschritt (2026-11-13)
# ======================================================================
# Jeder KI-Aufruf wird unter seinem Arbeitsschritt im Projekt mitgeschrieben (llm.LLM.bind → proj["cost_log"]),
# auch wenn ein Lauf abbricht. proj["cost_runs"] hält zusätzlich die letzten Läufe fest.

COST_RUNS_MAX = 40


def note_run(proj: dict, label: str, llm) -> None:
    """Hält die Kosten eines beendeten (oder abgebrochenen) Laufs im Projekt fest."""
    summary = llm.run_summary() if hasattr(llm, "run_summary") else {}
    if not summary:
        return
    runs = proj.setdefault("cost_runs", [])
    runs.append({"when": datetime.now(timezone.utc).isoformat(timespec="minutes"), "label": label,
                 "usd": round(sum(r.get("usd", 0.0) for r in summary.values()), 4), "steps": summary})
    del runs[:-COST_RUNS_MAX]


def cost_overview(proj: dict) -> dict:
    """Kosten des Projekts je Arbeitsschritt, teuerster zuerst: {"rows": [{"step", "calls", "batch", "searches",
    "usd", "list_usd"}], "total", "earlier", "saved", "runs"}. "earlier": Kosten aus der Zeit vor der
    Aufstellung (ältere Projekte); "saved": Ersparnis durch Sammelaufträge gegenüber dem Listenpreis."""
    rows = [{"step": step, "calls": int(r.get("calls", 0)), "batch": int(r.get("batch_requests", 0)),
             "searches": int(r.get("searches", 0)), "usd": float(r.get("usd", 0.0)),
             "list_usd": float(r.get("list_usd", 0.0))}
            for step, r in (proj.get("cost_log") or {}).items() if r.get("usd") or r.get("calls")]
    rows.sort(key=lambda r: -r["usd"])
    total = sum(r["usd"] for r in rows)
    return {"rows": rows, "total": total, "earlier": max(0.0, (proj.get("cost_usd") or 0.0) - total),
            "saved": max(0.0, sum(r["list_usd"] - r["usd"] for r in rows)),
            "runs": list(proj.get("cost_runs") or [])[-5:]}


# ======================================================================
# Vergleich „schlanke Würdigung“ für Kurznachrichten (Sparmaßnahme 2026-11-13)
# ======================================================================
# Die Autorin entscheidet anhand von drei Publikationen, ob die schlanke Würdigung für Kurznachrichten
# gleichwertig ist: je Publikation entsteht die Kurznachricht einmal aus der vollständigen und einmal aus der
# schlanken Würdigung, in zufälliger Reihenfolge („Fassung 1/2“), die Auflösung steht am Ende.

LEAN_TEST_N = 3
STEP_CITE_FIX = "Literaturziffern der Würdigung angleichen"


def run_lean_comparison(llm: LLM, proj: dict, log, n: int = LEAN_TEST_N) -> None:
    """Ergebnis in proj["lean_test"] (Word-Datei: lean_comparison_docx). Vollständige Würdigungen, die dabei neu
    entstehen, bleiben als Würdigung der Publikation im Projekt und werden später nicht noch einmal bezahlt.
    Bricht der Vergleich ab, setzt ein erneuter Start dort fort (proj["lean_test_work"])."""
    apply_manual_limits(proj)
    pubs = _pub_list(proj)
    if not pubs:
        raise ValueError("Es sind noch keine Publikationen ausgewählt.")
    topic_of = dict(pubs)
    work = proj.setdefault("lean_test_work", {})
    chosen = [p for p in work.get("pmids", []) if p in topic_of]
    if not chosen:
        tiers, grouped = _preview_tiers(llm, proj, pubs, log, True, {"replan_tiers": True})
        singles = [p for p, _ in pubs if p not in grouped]

        def score(p):
            return proj["topics"][topic_of[p]].get("ranking", {}).get(p, {}).get("length_score", 50)

        def has_full(p):
            return bool(proj["pubs"].get(p, {}).get("appraisal_en")) and not _is_lean(proj, p)

        # zuerst geplante Kurznachrichten (bevorzugt mit vorhandener Würdigung, dann die wichtigsten), sonst
        # die Publikationen mit dem niedrigsten Längenwert
        news = sorted((p for p in singles if tiers.get(p) == "Kurznachricht"),
                      key=lambda p: (not has_full(p), -score(p)))
        rest = sorted((p for p in singles if p not in news), key=score)
        chosen = (news + rest)[:n]
        work.clear()
        work.update({"pmids": chosen, "items": {}})
    items = work.setdefault("items", {})
    for p in chosen:
        items.setdefault(p, {})
    log(f"Vergleich für {len(chosen)} Publikation(en): vollständige und schlanke Würdigung, daraus je eine "
        "Kurznachricht…")
    _fetch_fulltexts(proj, chosen)

    # 1. Vollständige Würdigung (vorhandene werden verwendet; eine vorhandene schlanke wird ersetzt und für den
    # Vergleich aufgehoben)
    for p in chosen:
        if _is_lean(proj, p) and "lean_en" not in items[p]:
            items[p]["lean_en"] = proj["pubs"][p]["appraisal_en"]
    _appraise(llm, proj, [(p, topic_of[p]) for p in chosen if not proj["pubs"].get(p, {}).get("appraisal_en")],
              set(), log, False)
    _appraise(llm, proj, [(p, topic_of[p]) for p in chosen if _is_lean(proj, p)], set(), log, False, replace=True)
    chosen = [p for p in chosen if proj["pubs"][p].get("appraisal_en") and not _is_lean(proj, p)]
    if not chosen:
        raise RuntimeError("Für den Vergleich konnte keine vollständige Würdigung erstellt werden (siehe Hinweise "
                           "aus der Verarbeitung).")
    for p in chosen:
        _set_body(proj, p)

    # 2. Schlanke Würdigung
    def lean(p):
        entry, _ = _appraisal_inputs(proj, p, topic_of[p], with_docs=False)
        return A.generate_lean_appraisal(llm, entry, "Englisch")

    for p, res in _parallel([(p, p) for p in chosen if "lean_en" not in items[p]], lean, log,
                            "Schlanke Würdigung").items():
        if isinstance(res, Exception):
            _err(proj, f"Schlanke Würdigung {proj['records'].get(p, {}).get('title', p)[:60]}", res)
        else:
            items[p]["lean_en"] = res

    # 3. Kurznachricht aus beiden Würdigungen, dann Übersetzung (wie im Manuskript, ohne Stilanpassung)
    def news_of(job):
        p, kind = job
        body = proj["pubs"][p]["body_en"] if kind == "full" else strip_appraisal_preamble(items[p]["lean_en"])
        en = A.kurznachricht(llm, body, "Englisch", proj["records"].get(p, {}).get("abstract") or "")
        return {"en": en, "de": A.translate_structured(llm, en)}

    todo = [((p, kind), (p, kind)) for p in chosen for kind in ("full", "lean")
            if not (items[p].get(kind) or {}).get("de") and (kind == "full" or items[p].get("lean_en"))]
    for (p, kind), res in _parallel(todo, news_of, log, "Kurznachricht").items():
        if isinstance(res, Exception):
            _err(proj, f"Kurznachricht für den Vergleich {p}", res)
        else:
            items[p][kind] = res

    done = [p for p in chosen if (items[p].get("full") or {}).get("de") and (items[p].get("lean") or {}).get("de")]
    if not done:
        raise RuntimeError("Der Vergleich konnte nicht erstellt werden (siehe Hinweise aus der Verarbeitung).")
    summary = llm.run_summary() if hasattr(llm, "run_summary") else {}
    full_calls = summary.get(STEP_APPRAISAL, {}).get("calls", 0)
    lean_calls = summary.get(STEP_APPRAISAL_LEAN, {}).get("calls", 0)
    costs = dict((proj.get("lean_test") or {}).get("costs") or {})  # Messwerte eines früheren Teil-Laufs behalten
    if full_calls:
        costs["full_usd"] = round((summary[STEP_APPRAISAL].get("usd", 0.0)
                                   + summary.get(STEP_CITE_FIX, {}).get("usd", 0.0)) / full_calls, 3)
        costs["full_n"] = full_calls
    if lean_calls:
        costs["lean_usd"] = round(summary[STEP_APPRAISAL_LEAN].get("usd", 0.0) / lean_calls, 3)
        costs["lean_n"] = lean_calls
    out = []
    for p in done:
        rec = proj["records"].get(p, {})
        first = random.Random(f"{proj.get('id', '')}|{p}").choice(["full", "lean"])
        out.append({"pmid": p, "title": rec.get("title", p), "journal": rec.get("journal", ""),
                    "year": rec.get("year", ""), "first": first,
                    "full_de": items[p]["full"]["de"], "lean_de": items[p]["lean"]["de"],
                    "full_en": items[p]["full"]["en"], "lean_en": items[p]["lean"]["en"]})
    proj["lean_test"] = {"created": datetime.now(timezone.utc).isoformat(timespec="minutes"), "items": out,
                         "costs": costs, "version": APP_VERSION}
    proj.pop("lean_test_work", None)
    log("Vergleich fertig.")


def lean_comparison_docx(proj: dict) -> bytes | None:
    """Word-Datei zum Vergleich: je Publikation „Fassung 1“ und „Fassung 2“ der Kurznachricht ohne Hinweis auf
    die Herkunft; Auflösung und gemessene Kosten auf der letzten Seite."""
    test = proj.get("lean_test") or {}
    if not test.get("items"):
        return None
    from io import BytesIO
    from docx import Document
    doc = Document()
    doc.add_heading("Vergleich: Kurznachricht aus vollständiger und aus schlanker Würdigung", level=1)
    doc.add_paragraph(
        "Je Publikation stehen hier zwei Fassungen der Kurznachricht, so wie sie im Manuskript erscheinen würden "
        "(vor der Anpassung an Ihren Schreibstil). Eine beruht auf der vollständigen kritischen Würdigung mit "
        "Websuche, die andere auf der schlanken Würdigung ohne Websuche. Die Reihenfolge ist zufällig. Bitte "
        "notieren Sie je Publikation: gleichwertig, Fassung 1 besser oder Fassung 2 besser. Die Auflösung steht "
        "auf der letzten Seite.")
    for i, it in enumerate(test["items"], start=1):
        doc.add_heading(f"{i}. {it['title']}", level=2)
        doc.add_paragraph().add_run(f"{it.get('journal', '')} {it.get('year', '')} · PMID {it['pmid']}".strip()
                                    ).italic = True
        order = [it["first"], "lean" if it["first"] == "full" else "full"]
        for k, kind in enumerate(order, start=1):
            para = doc.add_paragraph()
            para.add_run(f"Fassung {k}: ").bold = True
            para.add_run(it[f"{kind}_de"])
        doc.add_paragraph("Urteil: ☐ gleichwertig   ☐ Fassung 1 besser   ☐ Fassung 2 besser")
    doc.add_page_break()
    doc.add_heading("Auflösung", level=1)
    names = {"full": "vollständige Würdigung", "lean": "schlanke Würdigung"}
    for i, it in enumerate(test["items"], start=1):
        other = "lean" if it["first"] == "full" else "full"
        doc.add_paragraph(f"{i}. Fassung 1 = {names[it['first']]}, Fassung 2 = {names[other]}")
    doc.add_heading("Gemessene KI-Kosten je Würdigung", level=2)
    c = test.get("costs") or {}
    usd = lambda v: f"{v:.2f}".replace(".", ",")  # noqa: E731
    if c.get("full_usd") is not None:
        doc.add_paragraph(f"Vollständige Würdigung: ≈ {usd(c['full_usd'])} USD (Mittel aus {c.get('full_n', 1)} "
                          "Würdigung(en) dieses Vergleichs, direkter Aufruf).")
    else:
        doc.add_paragraph("Vollständige Würdigung: in diesem Vergleich nicht neu erstellt (die vorhandenen wurden "
                          "verwendet); die Kosten stehen in der Kostenaufstellung der App.")
    if c.get("lean_usd") is not None:
        doc.add_paragraph(f"Schlanke Würdigung: ≈ {usd(c['lean_usd'])} USD (Mittel aus {c.get('lean_n', 1)} "
                          "Würdigung(en) dieses Vergleichs, direkter Aufruf).")
    if c.get("full_usd") is not None and c.get("lean_usd") is not None:
        doc.add_paragraph(f"Ersparnis je Kurznachricht: ≈ {usd(max(0.0, c['full_usd'] - c['lean_usd']))} USD. Im "
                          "Sparmodus (Sammelauftrag) halbieren sich beide Werte ungefähr.")
    doc.add_paragraph(f"Erstellt am {test.get('created', '')[:10]}, Programmstand {test.get('version', '')}.")
    bio = BytesIO()
    doc.save(bio)
    return bio.getvalue()


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


FAZIT_SCHEMA = obj({"items": arr(obj({"id": STR, "sentence": STR}))})
_FAZIT_RE = re.compile(r"^(Fazit für die Praxis:\s*)(.+?[.!?])(\s+|$)(.*)$", re.S)


def _de_text_key(d: dict) -> str:
    return "styled_de" if d.get("styled_de") else "short_de"


def vary_fazit_openings(llm: LLM, proj: dict, ready: list[tuple[str, str]], log) -> int:
    """Fangen mehrere Fazits gleich an (z.B. "Das Ergebnis ist neu."), wird nur deren erster Satz in einem
    günstigen KI-Aufruf abwechslungsreich und konkret umformuliert. Rückgabe: Anzahl geänderter Fazits."""
    found = {}
    for p, _ in ready:
        d = proj["pubs"][p]
        key = _de_text_key(d)
        lines = (d.get(key) or "").split("\n")
        for i, line in enumerate(lines):
            m = _FAZIT_RE.match(line.strip())
            if m:
                found[p] = (key, i, m)
                break

    def start(m):
        return " ".join(re.findall(r"\w+", m.group(2).lower())[:3])

    counts: dict[str, int] = {}
    for _, _, m in found.values():
        counts[start(m)] = counts.get(start(m), 0) + 1
    dup = [p for p, (_, _, m) in found.items() if counts[start(m)] > 1]
    if len(dup) < 2:
        return 0
    log(f"Fazit-Formulierungen werden abwechslungsreicher gestaltet ({len(dup)})…")
    items = "\n\n".join(f"id {k}: {found[p][2].group(2)} {found[p][2].group(4)}".strip() for k, p in enumerate(dup))
    res = llm.json(P.vary_fazit_prompt(items), FAZIT_SCHEMA, model=MODEL_FAST, max_tokens=6000, effort="medium")
    changed = 0
    for it in res.get("items", []):
        try:
            p = dup[int(re.sub(r"\D", "", it.get("id", "")) or -1)]
        except (ValueError, IndexError):
            continue
        sent = (it.get("sentence") or "").strip()
        if not sent:
            continue
        key, i, m = found[p]
        d = proj["pubs"][p]
        lines = d[key].split("\n")
        lines[i] = (m.group(1) + sent + (" " + m.group(4) if m.group(4) else "")).rstrip()
        d[key] = "\n".join(lines)
        changed += 1
    return changed


HEADINGS_SCHEMA = obj({"items": arr(obj({"id": STR, "de": STR}))})


HEADINGS_VERSION = 3  # 2: nach dem Inhalt der Publikationen; 3: deutsche Überschriften der Autoren bleiben wörtlich


def headings_de(llm: LLM, proj: dict, topic_ids: list[str]) -> dict[str, str]:
    """Deutsche Überschriften (Kapitel, Abschnitte, Unterabschnitte, Themen) für das deutsche Manuskript,
    auch wenn das Inhaltsverzeichnis englisch hochgeladen wurde. Nicht wörtlich übersetzt, sondern als
    deutscher Fachbegriff für das, worum es in den besprochenen Publikationen tatsächlich geht (Wunsch der
    Autorin, 2026-10-13). Ein günstiger KI-Aufruf; neu nur, wenn sich Überschriften oder Publikationen ändern."""
    by_topic: dict[str, list[str]] = {}
    for p, tid in _pub_list(proj):
        if tid in topic_ids:
            by_topic.setdefault(tid, []).append((proj["records"].get(p, {}).get("title") or "")[:160])
    ctx: dict[str, dict] = {}  # Überschrift -> {"level", "focus", "pubs"}
    chapter = (proj.get("toc") or {}).get("chapter_title") or ""
    if chapter.strip():
        ctx[chapter] = {"level": "Kapitel", "focus": "", "pubs": []}
    for tid in topic_ids:
        if tid in (SAFETY, VARIA):  # eigene Schlagwort-Überschriften je Arbeit (keyword_headings)
            continue
        t = proj["topics"][tid]
        pubs = by_topic.get(tid, [])
        for level, src, focus in (("Abschnitt (x.x)", t.get("section_title") or "", ""),
                                  ("Unterabschnitt", t.get("subsection_title") or "", ""),
                                  ("Thema", t["title"], t.get("english_search_focus") or "")):
            if not src.strip() or src == "Varia":
                continue
            c = ctx.setdefault(src, {"level": level, "focus": focus, "pubs": []})
            c["pubs"] += [x for x in pubs if x not in c["pubs"]]
    key = [f"v{HEADINGS_VERSION}"] + sorted(f"{k}|{'|'.join(sorted(v['pubs']))}" for k, v in ctx.items())
    cache = proj.get("headings_de") or {}
    if proj.get("headings_de_for") == key:
        return cache
    srcs = list(ctx)
    items = "\n\n".join(
        f"{i}: {src}\n   Ebene: {ctx[src]['level']}"
        + (f"\n   Suchschwerpunkt: {ctx[src]['focus']}" if ctx[src]["focus"] else "")
        + ("\n   Besprochene Publikationen:\n" + "\n".join(f"   - {x}" for x in ctx[src]["pubs"][:12])
           if ctx[src]["pubs"] else "")
        for i, src in enumerate(srcs))
    res = llm.json(P.headings_de_prompt(items), HEADINGS_SCHEMA, model=MODEL_FAST, max_tokens=4000,
                   effort="medium")
    new = {}
    for it in res.get("items", []):
        try:
            src = srcs[int(it.get("id", ""))]
        except (ValueError, IndexError):
            continue
        if it.get("de", "").strip():
            new[src] = it["de"].strip()
    proj["headings_de"], proj["headings_de_for"] = new, key
    return new


KEYWORD_SCHEMA = obj({"headings": arr(obj({"id": STR, "de": STR, "en": STR}))})
KEYWORD_VERSION = 1


def keyword_headings(llm: LLM, proj: dict, tid: str, pmids: list[str]) -> list[tuple[str, str, list[str]]]:
    """„Neue Themen“ und „Varia“: jede Arbeit erhält eine eigene Überschrift mit ihrem Schlagwort/Thema;
    Arbeiten zur selben Fragestellung teilen sich eine (Entscheidung der Autorin 2026-11-13).
    Rückgabe [(Überschrift deutsch, englisch, [pmids])] in der Reihenfolge der ersten Arbeit."""
    if not pmids:
        return []
    key = [f"v{KEYWORD_VERSION}"] + sorted(pmids)
    t = proj["topics"][tid]
    cache = t.get("keyword_headings") or {}
    if cache.get("for") != key:
        lines = []
        for i, p in enumerate(pmids):
            d, rec = proj["pubs"].get(p, {}), proj["records"].get(p, {})
            title = d.get("title_en") or rec.get("title") or p
            q = (t.get("ranking", {}).get(p) or {}).get("question_key") or ""
            lines.append(f"{i}: {title[:220]}" + (f"\n   Fragestellung: {q}" if q else ""))
        res = llm.json(P.keyword_headings_prompt("\n".join(lines)), KEYWORD_SCHEMA, model=MODEL_FAST,
                       max_tokens=3000, effort="low")
        items = {}
        for it in res.get("headings", []):
            try:
                p = pmids[int(it.get("id", ""))]
            except (ValueError, IndexError):
                continue
            if (it.get("de") or "").strip():
                items[p] = [it["de"].strip(), (it.get("en") or it["de"]).strip()]
        cache = {"for": key, "items": items}
        t["keyword_headings"] = cache
    groups: dict[str, list] = {}
    for p in pmids:
        de, en = cache["items"].get(p) or [proj["records"].get(p, {}).get("title", p)[:80]] * 2
        g = groups.setdefault(de.lower(), [de, en, []])
        g[2].append(p)
    return [tuple(g) for g in groups.values()]


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
    for tid in topic_order(proj):
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


GROUP_SCHEMA = obj({"groups": arr(obj({"pub_ids": arr(STR), "question_de": STR, "question_en": STR}))})


def find_groups(llm: LLM, proj: dict, tid: str, pmids: list[str]) -> list[dict]:
    """Publikationen eines Themas mit gleicher Fragestellung (je Gruppe 2 bis MAX_GROUP_SIZE)."""
    rk = proj["topics"][tid].get("ranking", {})
    pubs = [{"id": p, "title": proj["records"].get(p, {}).get("title", p),
             "question": rk.get(p, {}).get("question_key", ""),
             "summary": " ".join((proj["records"].get(p, {}).get("abstract") or "").split())[:500]} for p in pmids]
    hints = [g["pmids"] for g in proj.get("question_groups", []) if all(p in pmids for p in g["pmids"])]
    res = llm.json(P.group_prompt(proj["topics"][tid]["title"], pubs, hints), GROUP_SCHEMA, model=MODEL_MAIN,
                   max_tokens=3000, effort="medium")
    out, used = [], set()
    for g in res.get("groups", []):
        ids = [p for p in dict.fromkeys(g.get("pub_ids", [])) if p in pmids and p not in used]
        if 2 <= len(ids) <= MAX_GROUP_SIZE:
            used.update(ids)
            out.append({"pmids": ids, "question_de": g.get("question_de", "").strip(),
                        "question_en": g.get("question_en", "").strip()})
    return out


def apply_groups(llm: LLM, proj: dict, ready: list[tuple[str, str]], log) -> list[tuple[str, str]]:
    """Ersetzt Publikationen mit nahezu gleicher Fragestellung durch EINE gemeinsame Würdigung
    (Eintrag "G-…" in proj["pubs"], gebildet aus den Einzelwürdigungen). Gibt die neue Liste zurück."""
    by_topic = _singles_by_topic(proj, ready)
    _ensure_groups(llm, proj, by_topic, log)
    valid = _valid_groups(proj, by_topic)
    todo = [(gid, (gid, tid, g)) for gid, (tid, g) in valid.items()
            if not proj["pubs"].get(gid, {}).get("appraisal_en")]

    def joint(item):
        gid, tid, g = item
        members = []
        for m in g["pmids"]:
            rec, d = proj["records"].get(m, {}), proj["pubs"][m]
            lit = "\n".join(f"{i}. {e}" for i, e in enumerate(d.get("literature", [])[1:], start=2))
            members.append({"title": rec.get("title", m), "journal": rec.get("journal", ""), "year": rec.get("year", ""),
                            "pub_types": ", ".join(rec.get("pub_types") or []),
                            "appraisal": d["body_en"] + ("\n\nReferences\n" + lit if lit else "")})
        return A.joint_appraisal(llm, g["question_en"] or proj["topics"][tid]["title"], members)

    if todo:
        log(f"Gemeinsame Würdigungen werden erstellt ({len(todo)})…")
    for gid, res in _parallel(todo, joint, log, "Gemeinsame Würdigung").items():
        if isinstance(res, Exception):
            _err(proj, "Gemeinsame Würdigung", res)
            continue
        tid, g = valid[gid]
        body, entries = extract_and_renumber_citations_global(res, len(g["pmids"]) + 1)
        mains = [pubmed.format_reference(proj["records"].get(m, {})) for m in g["pmids"]]
        proj["pubs"][gid] = {"group": g["pmids"], "topic_id": tid, "appraisal_en": res,
                             "body_en": strip_appraisal_preamble(body), "literature": mains + entries,
                             "title_de": f"Gemeinsame Würdigung: {g['question_de'] or g['question_en']}",
                             "title_en": f"Joint appraisal: {g['question_en'] or g['question_de']}"}
    # Reihenfolge: die gemeinsame Würdigung steht an der Stelle ihrer ersten Publikation
    member_of = {m: gid for gid in valid if proj["pubs"].get(gid, {}).get("body_en")
                 for m in proj["pubs"][gid]["group"]}
    out, seen = [], set()
    for p, t in ready:
        gid = member_of.get(p, p)
        if gid not in seen:
            seen.add(gid)
            out.append((gid, t))
    return out


OWN_REFS_MAX = 2  # Mindestzahl; tatsächlich höchstens einer je Thema, siehe own_refs_limit (2026-10-15)
OWN_REFS_CAP = 12


def own_refs_limit(n_topics: int) -> int:
    return min(OWN_REFS_CAP, max(OWN_REFS_MAX, n_topics))


OWN_REFS_SCHEMA = obj({"refs": arr(obj({"pub_id": STR, "manuscript": STR, "sentence_de": STR,
                                        "sentence_en": STR}))})


def find_own_refs(llm: LLM, proj: dict, ready: list[tuple[str, str]]) -> dict[str, dict]:
    """Ein KI-Aufruf über alle früheren Manuskripte und alle Publikationen: höchstens ein Rückbezug je Thema
    (insgesamt own_refs_limit) {pub_id: {"manuscript", "de", "en"}}. Vorbild Decker: zehn Rückbezüge auf
    frühere Ausgaben in einem Kapitel (2026-10-15)."""
    manuscripts = [(n, t[:15000]) for n, t in list(proj["style"].items())[:4]]
    topic_of = {p: t for p, t in ready}
    limit = own_refs_limit(len(set(topic_of.values())))
    pubs = [{"id": p, "topic": proj["topics"][t]["title"], "title": proj["records"].get(p, {}).get("title", p),
             "summary": " ".join((proj["pubs"][p].get("short_en") or "").split())[:500]} for p, t in ready]
    res = llm.json(P.own_refs_prompt(manuscripts, pubs, limit), OWN_REFS_SCHEMA, model=MODEL_MAIN,
                   max_tokens=6000, effort="medium")
    out, used_topics = {}, set()
    for r in res.get("refs", []):
        pid = r.get("pub_id")
        if pid in proj["pubs"] and r.get("sentence_de", "").strip() and len(out) < limit \
                and topic_of.get(pid) not in used_topics:
            used_topics.add(topic_of.get(pid))
            out[pid] = {"manuscript": r.get("manuscript", ""), "de": r["sentence_de"].strip(),
                        "en": r.get("sentence_en", "").strip()}
    return out


def _text_sig(text: str | None) -> str:
    """Kurze Kennung eines Textes für Cache-Schlüssel (ändert sich der Text, wird neu erzeugt)."""
    text = text or ""
    return f"{len(text)}:{hashlib.md5(text.encode('utf-8')).hexdigest()[:10]}"


TOPIC_COMMENT_SCHEMA = obj({"comment_de": STR, "comment_en": STR})
TRANSITIONS_SCHEMA = obj({"transitions": arr(obj({"pub_id": STR, "de": STR, "en": STR}))})
# State of the Art gestrichen (Entscheidung der Autorin 2026-11-03); an seine Stelle treten die Überleitungen
# vor der ersten Publikation jedes Themas. Code bleibt für eine mögliche Wiederaufnahme erhalten.
SOTA_ENABLED = False
TRANSITIONS_VERSION = 2
LONG_TIERS = ("1 Seite (Lang)", "1,5 Seiten (Lang)", "2 Seiten")
SHORT_TIERS = ("½ Seite (Kurz)", "⅓ Seite (Kurz)")


def topic_comments(llm: LLM, proj: dict, ready: list[tuple[str, str]], log) -> None:
    """Ein gemeinsamer Kommentar je Thema mit 2–4 kurz gewürdigten Publikationen (½ Seite), sofern dort keine
    ausführliche Würdigung mit eigenem Kommentar steht (Entscheidung der Autorin 2026-10-15, Vorbild Decker).
    Ergebnis in proj["topics"][tid]["topic_comment"] = {"key", "de", "en"}; Cache über die beteiligten Texte."""
    todo = []
    for tid in dict.fromkeys(t for _, t in ready):
        ids = [p for p, t in ready if t == tid]
        short = [p for p in ids if proj["tiers"].get(p) in SHORT_TIERS and proj["pubs"][p].get("short_en")]
        has_long = any(proj["tiers"].get(p) in LONG_TIERS for p in ids)
        t = proj["topics"][tid]
        if not (2 <= len(short) <= 4) or has_long or t.get("varia") or t.get("safety") or tid in (VARIA, SAFETY):
            t.pop("topic_comment", None)
            continue
        key = [SHORT_VERSION] + [(p, _text_sig(proj["pubs"][p].get("short_en"))) for p in short]
        if (t.get("topic_comment") or {}).get("key") != key:
            todo.append((tid, (tid, short, key)))

    def work(item):
        tid, short, key = item
        title = proj["topics"][tid]["title"]
        items = [(proj["records"].get(p, {}).get("title", p), proj["pubs"][p]["short_en"]) for p in short]
        res = llm.json(P.topic_comment_prompt(title, items, _guideline_docs(proj, tid, title, per_doc=3000, total=6000)),
                       TOPIC_COMMENT_SCHEMA, model=MODEL_MAIN, max_tokens=3000, effort="medium")
        return {"key": key, "de": res.get("comment_de", "").strip(), "en": res.get("comment_en", "").strip()}

    if not todo:
        return
    log(f"Gemeinsame Kommentare für kurz gewürdigte Publikationen ({len(todo)} Thema/Themen)…")
    for tid, res in _parallel(todo, work, log, "Gemeinsamer Kommentar").items():
        if isinstance(res, Exception):
            _err(proj, f"Gemeinsamer Kommentar {proj['topics'][tid]['title']}", res)
        elif res["de"]:
            proj["topics"][tid]["topic_comment"] = res


def _first_paragraph(text: str) -> str:
    for line in text.split("\n"):
        line = re.sub(r"^\**\s*(Studie|Study|Publikation \d|Übersicht der Publikationen)\s*:?\**\s*", "", line.strip())
        if line:
            return line
    return ""


def transition_slots(proj: dict, ready: list[tuple[str, str]], tid: str) -> tuple[list[str], list[str]]:
    """Publikationen eines Themas in Lesereihenfolge (reguläre zuerst, Kurznachrichten am Ende) und die Pflicht-
    Stellen für eine Überleitung (Entscheidung der Autorin 2026-11-03): vor der ersten Publikation des Themas,
    vor jeder gemeinsamen Würdigung und vor jeder Würdigung der Stufe Lang."""
    ids = [p for p, t in ready if t == tid and proj["pubs"][p].get("short_en")]
    regular = [p for p in ids if proj["tiers"].get(p) != "Kurznachricht"]
    kurz = [p for p in ids if proj["tiers"].get(p) == "Kurznachricht"]
    order = regular + kurz
    required = [p for p in regular if proj["pubs"][p].get("group") or proj["tiers"].get(p) in LONG_TIERS]
    if order and order[0] not in required:
        required.insert(0, order[0])
    return (regular if regular else order[:1]), required


def find_transitions(llm: LLM, proj: dict, ready: list[tuple[str, str]], log) -> None:
    """Überleitungen zwischen den Publikationen eines Themas (Entscheidung der Autorin 2026-11-03): immer vor der
    ersten Publikation des Themas (ersetzt den State of the Art), vor jeder gemeinsamen Würdigung und vor jeder
    Würdigung der Stufe Lang; zusätzlich gelegentlich (höchstens eine je drei weitere Publikationen, nur bei Themen
    mit mindestens drei regulären Publikationen), nie vor Kurznachrichten außer als erste des Themas.
    Ein Sonnet-Aufruf je Thema; Cache in proj["topics"][tid]["transitions"] = {"key", "items": {pub_id: {"de", "en"}}}."""
    todo = []
    topic_order = list(dict.fromkeys(t for _, t in ready))
    for i, tid in enumerate(topic_order):
        ids, required = transition_slots(proj, ready, tid)
        t = proj["topics"][tid]
        if not ids:
            t.pop("transitions", None)
            continue
        n_opt = (len(ids) - len(required)) // 3 if len(ids) >= 3 else 0
        prev = proj["topics"][topic_order[i - 1]]["title"] if i else ""
        key = [TRANSITIONS_VERSION, prev, required, n_opt] + \
              [(p, _text_sig(proj["pubs"][p].get("styled_de") or proj["pubs"][p].get("short_de"))) for p in ids]
        if (t.get("transitions") or {}).get("key") != key:
            todo.append((tid, (tid, ids, required, n_opt, prev, key)))

    def work(item):
        tid, ids, required, n_opt, prev, key = item
        title = proj["topics"][tid]["title"]
        items = [(p, proj["records"].get(p, {}).get("title", p),
                  _first_paragraph(proj["pubs"][p].get("short_de") or proj["pubs"][p]["short_en"]),
                  "gemeinsam" if proj["pubs"][p].get("group") else
                  ("lang" if proj["tiers"].get(p) in LONG_TIERS else "")) for p in ids]
        res = llm.json(P.transitions_prompt(title, items, required, n_opt, prev), TRANSITIONS_SCHEMA,
                       model=MODEL_FAST, max_tokens=2500, effort="medium")
        out, n_free = {}, 0
        for r in res.get("transitions", []):
            pid = str(r.get("pub_id", ""))
            if pid not in ids or pid in out or not r.get("de", "").strip():
                continue
            if pid not in required:
                if pid == ids[0] or n_free >= n_opt:
                    continue
                n_free += 1
            out[pid] = {"de": re.sub(r"\s*[(\[]\d+(?:\s*,\s*\d+)*[)\]]", "", r["de"]).strip(),
                        "en": re.sub(r"\s*[(\[]\d+(?:\s*,\s*\d+)*[)\]]", "", r.get("en", "")).strip()}
        return {"key": key, "items": out}

    if not todo:
        return
    log(f"Überleitungen zwischen den Publikationen ({len(todo)} Thema/Themen)…")
    for tid, res in _parallel(todo, work, log, "Überleitungen").items():
        if isinstance(res, Exception):
            _err(proj, f"Überleitungen {proj['topics'][tid]['title']}", res)
        else:
            proj["topics"][tid]["transitions"] = res


def prepend_transition(lines: list[str], sentence: str) -> list[str]:
    """Setzt den Überleitungssatz vor die Hinleitung (erste Zeile ohne Label); beginnt die Würdigung mit einem
    Label, wird er eigener erster Absatz."""
    if not sentence or not lines:
        return lines
    first = lines[0].replace("**", "").strip()
    if any(first.lower().startswith(l.lower()) for l in _TEXT_LABELS):
        return [sentence] + lines
    return [sentence + " " + lines[0].strip()] + lines[1:]


_TEXT_LABELS = ("Studie:", "Ergebnisse:", "Schlussfolgerung der Autoren:", "Kommentar:", "Fazit für die Praxis:",
                "Study:", "Results:", "Authors' Conclusion:", "Comment:", "Conclusion for Practice:",
                "Übersicht der Publikationen", "Overview of the Publications", "Publikation 1", "Publication 1",
                "Gemeinsame Würdigung", "Joint Appraisal")


def insert_own_ref(text: str, sentence: str) -> str:
    """Setzt den Rückbezug als eigenen Satz vor das Fazit bzw. den Kommentar, sonst vor den letzten Satz."""
    if not sentence:
        return text
    lines = text.split("\n")
    idx = next((i for i, l in enumerate(lines)
                if re.match(r"^\**\s*(Kommentar|Fazit für die Praxis|Comment|Conclusion for Practice)\s*:", l.strip())), None)
    if idx is not None:
        j = max((i for i in range(idx) if lines[i].strip()), default=None)
        abstract_label = re.compile(r"^\**\s*(Studie|Ergebnisse|Schlussfolgerung der Autoren|Study|Results|"
                                    r"Authors'? Conclusion)\s*:", re.IGNORECASE)
        if j is not None and not abstract_label.match(lines[j].strip()):
            lines[j] = lines[j].rstrip() + " " + sentence
        else:
            lines.insert(idx, sentence)  # keine Würdigung davor: eigener Absatz vor dem Fazit
        return "\n".join(lines)
    j = max((i for i, l in enumerate(lines) if l.strip()), default=None)
    if j is None:
        return text
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ])", lines[j].strip())
    if len(parts) > 1:
        lines[j] = " ".join(parts[:-1] + [sentence, parts[-1]])
    else:
        lines[j] = lines[j].rstrip() + " " + sentence
    return "\n".join(lines)


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
    if not proj.get("id"):
        # Sehr alte Zwischenstände ohne Projektkennung: aus dem Inhalt ableiten, damit dieselbe Datei bei jedem
        # Laden dieselbe Kennung erhält (über sie findet die App einen laufenden Sammelauftrag wieder)
        proj["id"] = hashlib.md5(data).hexdigest()[:12]
    base = new_project()
    base.update(proj)
    return base
