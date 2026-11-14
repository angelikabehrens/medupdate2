"""Schritt 1: KI-gestützte Literaturempfehlung pro Thema (neuer Algorithmus).

Ablauf pro Thema:
1. Suchplan: Die KI leitet PICO und drei sich ergänzende PubMed-Strategien ab
   (präzise / breit / hohe Evidenz).
2. Suche: Alle drei Strategien laufen mit Zeitraum- und Sprachfilter; die Treffer werden
   abwechselnd zusammengeführt (Round Robin), damit keine Strategie die Kandidatenliste dominiert.
3. Mechanischer Filter: ohne Abstract, Kommentare, Editorials, Errata, Fallberichte usw. fallen raus.
4. Bewertung: Die KI bewertet jede Kandidatin nach Relevanz, Evidenz, Risk of Bias, Neuheit und
   Praxisrelevanz (Leitfrage des Handbuchs) und vergibt einen normierten Fragestellungs-Schlüssel.
5. Auswahl (deterministisch, nachvollziehbar): gewichteter Score, Relevanz-Schwelle, Abzug bei
   hohem Bias-Risiko, Bevorzugung von Originalarbeiten und höchstens eine Arbeit pro Fragestellung.
   Ausgewählt werden NUR Arbeiten der Priorität A; wie viele das je Thema sind, ergibt sich damit
   aus der Qualität der gefundenen Arbeiten (bis zu einer Obergrenze).
6. Längenwert: Studienqualität (Evidenz, Bias-Risiko) zuerst, dann Bedeutung der Fragestellung für
   das Themengebiet, nachrangig der aktuelle Impact Factor des Journals. Er bestimmt die Länge der
   Besprechung und welche Arbeiten bei Überschreitung der Gesamtlänge zuerst gekürzt oder
   zurückgestellt werden.
"""

from __future__ import annotations

import hashlib
import math
import re

from datetime import date, timedelta

from . import prompts_new as P
from . import pubmed
from .config import MAX_CANDIDATES_PER_TOPIC, MODEL_MAIN
from .llm import BOOL, INT, LLM, NUM, STEP_RANK, STR, Job, arr, enum, obj, run_job

EXCLUDED_TYPES = {
    "Comment", "Editorial", "Letter", "Erratum", "Published Erratum", "Retracted Publication",
    "Retraction of Publication", "News", "Newspaper Article", "Interview", "Biography", "Case Reports",
    "Portrait", "Congress", "Expression of Concern",
}

PLAN_SCHEMA = obj({
    "pico": STR,
    "queries": arr(obj({"name": enum("praezise", "breit", "hohe_evidenz"), "query": STR})),
    "exclusion": STR,
})

# Kapitelanalyse: Wie ist das Inhaltsverzeichnis gegliedert, welcher Suchbegriff gehört in jede Suche?
FRAME_SCHEMA = obj({
    "chapter_en": STR,
    "structure": enum("methode", "krankheit", "gemischt"),
    "structure_note": STR,
    "anchor_query": STR,
    "journals": arr(STR),
    "topics": arr(obj({"id": STR, "title_en": STR, "searchable": BOOL, "aspect_query": STR, "note": STR})),
})

RANK_SCHEMA = obj({
    "items": arr(obj({
        "pmid": STR,
        "study_design": STR,
        "relevance": INT,
        "evidence": INT,
        "risk_of_bias": enum("Low", "Some concerns", "High", "Unclear"),
        "novelty": INT,
        "practice_impact": INT,
        "question_importance": INT,
        "journal_impact_factor": NUM,
        "question_key": STR,
        "is_primary_research": BOOL,
        "rationale": STR,
    })),
})

WEIGHTS = {"relevance": 0.35, "evidence": 0.25, "practice_impact": 0.25, "novelty": 0.15}
RELEVANCE_MIN = 55
BIAS_PENALTY = {"Low": 0, "Some concerns": 3, "Unclear": 5, "High": 15}


def time_window(recency_months: int, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    months = recency_months if recency_months and recency_months > 0 else 12
    return today - timedelta(days=int(months * 30.4)), today


def topic_path(topic: dict) -> str:
    """Gliederungspfad eines Themas, z.B. „Rupturiertes AAA › Anästhesie und Zugangsart bei rEVAR“."""
    parts = [topic.get("section_title", ""), topic.get("subsection_title", ""), topic.get("title", "")]
    out = []
    for x in parts:
        x = (x or "").strip()
        if x and x not in out:
            out.append(x)
    return " › ".join(out)


def analyze_chapter(llm: LLM, chapter: str, topics: list[dict]) -> dict:
    """Ein Aufruf je Kapitel: englischer Titel, Art der Gliederung (Methode/Krankheit/gemischt), ein
    PubMed-Anker für alle Suchen, je Thema der englische Titel, ob es überhaupt suchbar ist, und ein
    optionaler Aspekt (z.B. Chirurgie), der nur bei passenden Themen dazukommt."""
    block = "\n".join(f"{t['id']}: {t.get('number', '')} {topic_path(t)}".strip() for t in topics)
    data = llm.json(P.chapter_frame_prompt(chapter, block), FRAME_SCHEMA, model=MODEL_MAIN, max_tokens=12000)
    known = {t["id"] for t in topics}
    data["topics"] = {x["id"]: x for x in data.get("topics", []) if x.get("id") in known}
    return data


def plan_search(llm: LLM, topic: dict, chapter: str, von: date, bis: date, frame: dict | None = None) -> dict:
    ft = ((frame or {}).get("topics") or {}).get(topic.get("id"), {})
    return llm.json(
        P.search_plan_prompt(topic["title"], topic.get("english_search_focus", ""), chapter,
                             von.strftime("%d.%m.%Y"), bis.strftime("%d.%m.%Y"),
                             path=topic_path(topic), title_en=ft.get("title_en", ""),
                             anchor=(frame or {}).get("anchor_query", ""), aspect=ft.get("aspect_query", ""),
                             structure=(frame or {}).get("structure", "")),
        PLAN_SCHEMA, model=MODEL_MAIN, max_tokens=12000,
    )


def _suffix(von: date, bis: date) -> str:
    return f" AND {pubmed.date_filter(von, bis)} AND (english[la] OR german[la]) AND hasabstract"


def count_queries(queries: list[dict], von: date, bis: date, email: str) -> dict:
    """Trefferzahlen je Suchstring (nur PubMed, keine KI-Kosten)."""
    out = {}
    for q in queries:
        if not (q.get("query") or "").strip():
            continue
        try:
            out[q["name"]] = pubmed.search(f"({q['query']}){_suffix(von, bis)}", 0, email)[1]
        except Exception as ex:
            out[q["name"]] = f"Fehler: {ex}"
    return out


SAFETY_HIGH_EVIDENCE = ("(randomized controlled trial[pt] OR meta-analysis[pt] OR systematic[sb] OR "
                        "guideline[pt] OR practice guideline[pt] OR consensus[tiab])")


def safety_queries(frame: dict) -> list[dict]:
    """Sicherheitsnetz je Kapitel: findet auch Arbeiten zu Themen, die (noch) nicht im Inhaltsverzeichnis
    stehen. Hohe Evidenz, Top-Journale und der Anker allein."""
    anchor = (frame.get("anchor_query") or "").strip()
    if not anchor:
        return []
    qs = [{"name": "netz_evidenz", "query": f"({anchor}) AND {SAFETY_HIGH_EVIDENCE}"}]
    journals = [j.strip().replace('"', "") for j in frame.get("journals") or [] if j.strip()]
    top = ["N Engl J Med", "Lancet", "JAMA", "BMJ"]
    js = list(dict.fromkeys(top + journals))[:14]
    qs.append({"name": "netz_journale", "query": f"({anchor}) AND (" + " OR ".join(f'"{j}"[ta]' for j in js) + ")"})
    qs.append({"name": "netz_anker", "query": anchor})
    return qs


MIN_CANDIDATES = 8
BROAD_HITS = 1000          # ab so vielen Treffern einer Suchstrategie gilt ein Thema als breit
BROAD_CANDIDATES = 100     # breite Themen: so viele Kandidaten bewerten (sonst MAX_CANDIDATES_PER_TOPIC)
PRECISE_HITS = 150         # ab so vielen Treffern der präzisen Suche ebenfalls 100 Kandidaten
HUGE_PRECISE_HITS = 300    # mehr Treffer der präzisen Suche: sehr großes Thema
HUGE_CANDIDATES = 300      # sehr große Themen: so viele Kandidaten bewerten
EVIDENCE_FIRST_MAX = 200   # davon höchstens so viele aus der vorgezogenen Suche nach hoher Evidenz
EVIDENCE_FIRST = ("(randomized controlled trial[pt] OR meta-analysis[pt] OR systematic[sb] OR "
                  "clinical trial, phase ii[pt] OR clinical trial, phase iii[pt] OR registry[tiab] OR "
                  "registries[tiab] OR register[tiab])")


def candidate_limit(counts: dict | None) -> int:
    """Wie viele Kandidaten ein Thema bewertet (Angelika 2026-11-09: bei sehr breiten Suchen mehr als 60,
    damit Epidemiologie- und Lebensstilthemen nicht an der Kandidatenzahl scheitern; 2026-11-10 nach dem
    Vergleich pAVK interventionell: auch, wenn schon die präzise Suche mehr als 150 Treffer liefert).
    Angelika 2026-11-14 nach den Vergleichen Psoriasis und Atopie: mehr als 300 Treffer der präzisen Suche
    → bis zu 300 Kandidaten, randomisierte Studien, Metaanalysen und Register zuerst (evidence_first)."""
    counts = counts or {}
    nums = [v for v in counts.values() if isinstance(v, int)]
    precise = counts.get("praezise")
    if isinstance(precise, int) and precise > HUGE_PRECISE_HITS:
        return HUGE_CANDIDATES
    if (nums and max(nums) > BROAD_HITS) or (isinstance(precise, int) and precise >= PRECISE_HITS):
        return BROAD_CANDIDATES
    return MAX_CANDIDATES_PER_TOPIC


def broaden_search(llm: LLM, topic: dict, plan: dict) -> dict:
    failed = "\n".join(f"- {q['name']}: {q['query']}" for q in plan.get("queries", []))
    data = llm.json(P.search_broaden_prompt(topic["title"], topic.get("english_search_focus", ""), failed),
                    PLAN_SCHEMA, model=MODEL_MAIN, max_tokens=12000)
    data["pico"] = plan.get("pico") or data.get("pico", "")
    return data


def evidence_first(plan: dict, pmids: list[str], von: date, bis: date, email: str,
                   limit: int) -> tuple[list[str], int | str | None]:
    """Sehr große Themen: Die Treffer der präzisen Suche mit hoher Evidenz (randomisierte Studien,
    Metaanalysen, Phase-II/III-Studien, Register) kommen vor alle anderen Kandidaten. Sonst stünden große
    Studien wie REZOLVE-AD oder Registerarbeiten unter 1.000 Treffern oft hinter Platz 100 (Vergleich Atopie
    2026-11-14). Gibt (Kandidaten, Trefferzahl der Evidenzsuche) zurück; ohne präzise Suche unverändert."""
    precise = next((q.get("query") or "" for q in plan.get("queries", []) if q.get("name") == "praezise"), "")
    if not precise.strip():
        return pmids[:limit], None
    try:
        ids, count = pubmed.search(f"(({precise}) AND {EVIDENCE_FIRST}){_suffix(von, bis)}", EVIDENCE_FIRST_MAX, email)
    except Exception as ex:  # ohne Evidenzsuche bleibt es bei der bisherigen Reihenfolge
        return pmids[:limit], f"Fehler: {ex}"
    return list(dict.fromkeys(ids[:EVIDENCE_FIRST_MAX] + pmids))[:limit], count


def collect_candidates(plan: dict, von: date, bis: date, email: str,
                       limit: int = MAX_CANDIDATES_PER_TOPIC) -> tuple[list[str], dict]:
    """Führt alle Suchstrategien aus und mischt die Treffer im Reißverschlussverfahren."""
    suffix = _suffix(von, bis)
    per_query: list[list[str]] = []
    stats = {}
    # Die Strategie „hohe Evidenz“ zuerst: beim Reißverschluss kommen ihre Treffer bevorzugt in die Kandidaten
    queries = sorted(plan.get("queries", []), key=lambda q: "evidenz" not in (q.get("name") or "").lower())
    for q in queries:
        if not (q.get("query") or "").strip():
            continue
        try:
            ids, count = pubmed.search(f"({q['query']}){suffix}", limit, email)
        except Exception as ex:  # eine fehlerhafte Strategie soll die anderen nicht blockieren
            ids, count = [], f"Fehler: {ex}"
        per_query.append(ids)
        stats[q["name"]] = count
    errors = [v for v in stats.values() if isinstance(v, str)]
    if stats and len(errors) == len(stats):
        raise RuntimeError(f"PubMed nicht erreichbar oder Suche fehlerhaft ({errors[0]})")
    merged: list[str] = []
    seen = set()
    for i in range(max((len(x) for x in per_query), default=0)):
        for ids in per_query:
            if i < len(ids) and ids[i] not in seen:
                seen.add(ids[i])
                merged.append(ids[i])
    return merged[:limit], stats


def prefilter(records: list[dict]) -> list[dict]:
    out = []
    for r in records:
        if not r.get("abstract") or len(r["abstract"]) < 300:
            continue
        if set(r.get("pub_types", [])) & EXCLUDED_TYPES:
            continue
        if not pubmed.is_english_or_german(r.get("language", "")):
            continue
        out.append(r)
    return out


# Geschätzte Kosten je bewertetem Abstract im Sammelauftrag (gemessen 11/2026: ≈ 0,0085 USD im direkten Aufruf)
RANK_BATCH_USD = 0.0045


def rank_jobs(topic: dict, pico: str, records: list[dict], recency_text: str, batch_size: int = 15,
              key: str = "r") -> list[tuple[Job, set[str]]]:
    """Die Bewertungsaufträge eines Themas (je 15 Abstracts einer) als Daten: [(Auftrag, zulässige PMIDs)].
    Derselbe Auftrag läuft sofort (rank) oder im Sammelauftrag (pipeline.run_recommendations, Sparmodus)."""
    out = []
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        block = "\n".join(
            f"---\nPMID: {r['pmid']}\nTitel: {r['title']}\nJournal: {r['journal']} ({r['year']})\n"
            f"Publication Type (PubMed): {', '.join(r['pub_types'])}\nAbstract: {r['abstract'][:3500]}"
            for r in batch
        )
        valid = {r["pmid"] for r in batch}
        sig = hashlib.md5(",".join(sorted(valid)).encode("utf-8")).hexdigest()[:12]
        out.append((Job(key=f"{key}|{sig}", prompt=P.ranking_prompt(topic["title"], pico, block, recency_text),
                        step=STEP_RANK, model=MODEL_MAIN, max_tokens=32000, effort="medium", schema=RANK_SCHEMA,
                        est_usd=RANK_BATCH_USD * len(batch)), valid))
    return out


def rank_items(data: dict, valid: set[str]) -> dict[str, dict]:
    """Bewertungen einer KI-Antwort, beschränkt auf die angefragten PMIDs."""
    return {item["pmid"]: item for item in (data or {}).get("items", []) if item.get("pmid") in valid}


def rank(llm: LLM, topic: dict, pico: str, records: list[dict], recency_text: str,
         batch_size: int = 15) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for job, valid in rank_jobs(topic, pico, records, recency_text, batch_size):
        result.update(rank_items(run_job(llm, job), valid))
    return result


def score(item: dict) -> float:
    s = sum(item.get(k, 0) * w for k, w in WEIGHTS.items())
    s -= BIAS_PENALTY.get(item.get("risk_of_bias", "Unclear"), 5)
    if not item.get("is_primary_research", True):
        s -= 10
    return round(s, 1)


def priority_class(item: dict) -> str:
    sc = item.get("score", 0)
    if sc >= 72 and item.get("practice_impact", 0) >= 65:
        return "A"
    if sc >= 60:
        return "B"
    return "C"


def quality(item: dict) -> float:
    """Studienqualität: Evidenzstärke abzüglich Bias-Risiko (0–100)."""
    q = item.get("evidence", 0) - 2 * BIAS_PENALTY.get(item.get("risk_of_bias", "Unclear"), 5)
    return max(0.0, min(100.0, float(q)))


def impact_score(jif) -> float:
    """Impact Factor auf 0–100 (logarithmisch: IF 1 ≈ 25, 3 ≈ 50, 7 ≈ 75, ≥15 = 100)."""
    try:
        jif = float(jif or 0)
    except (TypeError, ValueError):
        return 0.0
    return min(100.0, 25 * math.log2(1 + max(jif, 0)))


def length_score(item: dict) -> float:
    """Rangfolge für Länge und Umfang: Qualität vor Bedeutung der Fragestellung vor Impact Factor."""
    return round(0.6 * quality(item) + 0.3 * item.get("question_importance", item.get("relevance", 0))
                 + 0.1 * impact_score(item.get("journal_impact_factor")), 1)


MAX_PER_QUESTION = 3


def select(ranking: dict[str, dict], n_max: int, reserve: int = 3) -> tuple[list[str], list[str]]:
    """Auswahl innerhalb eines Themas (Grundlage; die endgültige Empfehlung trifft seit 2026-11-09
    pipeline.select_chapter kapitelweit). Nur Priorität A; höchstens 3 Arbeiten pro Fragestellung (gemeinsame Würdigung) und höchstens n_max je Thema.
    Reserve (nicht vorausgewählt): weitere A-Arbeiten über der Obergrenze, sonst die besten B-Arbeiten."""
    for pmid, item in ranking.items():
        item["score"] = score(item)
        item["priority"] = priority_class(item)
        item["quality"] = quality(item)
        item["length_score"] = length_score(item)
    ordered = sorted(
        (p for p, it in ranking.items() if it.get("relevance", 0) >= RELEVANCE_MIN),
        key=lambda p: (ranking[p]["length_score"], ranking[p]["score"]), reverse=True,
    )
    chosen, backup, per_key = [], [], {}
    for p in ordered:
        key = (ranking[p].get("question_key") or p).strip().lower()
        n_key = per_key.get(key, 0)
        # Bis zu 3 Arbeiten zur selben Fragestellung: sie werden in Schritt 2 gemeinsam gewürdigt
        if ranking[p]["priority"] == "A" and len(chosen) < n_max and n_key < MAX_PER_QUESTION:
            chosen.append(p)
            per_key[key] = n_key + 1
        elif ranking[p]["priority"] in ("A", "B") and len(backup) < reserve and not n_key:
            backup.append(p)
    return chosen, backup


def manual_window(window: list[str] | None) -> tuple[date, date] | None:
    """Manuell eingestellter Suchzeitraum ["JJJJ-MM-TT", "JJJJ-MM-TT"] oder None."""
    if not window:
        return None
    von, bis = (date.fromisoformat(x) for x in window)
    return (von, bis) if von <= bis else (bis, von)


def search_window(rules: dict, window: list[str] | None = None) -> tuple[date, date]:
    return manual_window(window) or time_window(rules.get("recency_months", 0))


def window_warning(window: list[str] | None, prior_names: list[str] | None = None,
                   today: date | None = None) -> str:
    """Hinweis, wenn der manuelle Suchzeitraum vermutlich falsch eingestellt ist: Er endet in der Zukunft,
    oder er passt nicht zum Jahr im Dateinamen des Vorjahresmanuskripts (Manuskript aus Jahr J: Suchzeitraum
    beginnt spätestens in J und endet spätestens in J+1)."""
    w = manual_window(window)
    if not w:
        return ""
    von, bis = w
    today = today or date.today()
    reasons = []
    if bis > today:
        reasons.append(f"er endet in der Zukunft ({bis.strftime('%m/%Y')})")
    years = [int(y) for n in (prior_names or []) for y in re.findall(r"(?<!\d)(20\d\d)(?!\d)", n)]
    if years:
        y = max(years)
        if von.year > y or bis.year > y + 1:
            reasons.append(f"das Vorjahresmanuskript stammt aus {y}, dazu passt ein Zeitraum etwa von {y} bis {y + 1}")
    if not reasons:
        return ""
    return (f"Bitte den Suchzeitraum prüfen ({von.strftime('%m/%Y')} bis {bis.strftime('%m/%Y')}): "
            + "; ".join(reasons) + ".")


def with_manual(plan: dict, manual: str) -> dict:
    """Eigene Suchbegriffe der Autorin als zusätzliche Strategie (bleiben neben dem KI-Suchplan erhalten)."""
    manual = (manual or "").strip()
    qs = [q for q in plan.get("queries", []) if q.get("name") != "eigene"]
    if manual:
        qs.append({"name": "eigene", "query": manual})
    return {**plan, "queries": qs}


def recommend_for_topic(llm: LLM, topic: dict, chapter: str, rules: dict, email: str,
                        n_max: int, log=lambda msg: None, window: list[str] | None = None,
                        plan: dict | None = None, frame: dict | None = None,
                        known: dict | None = None, limit: int | None = None) -> dict:
    """known: bereits vorliegende Bewertungen dieses Themas (gleicher Suchplan); sie werden übernommen und
    nur neue Kandidaten bewertet. limit: Zahl der Kandidaten (Standard: nach den Trefferzahlen, candidate_limit)."""
    prep = prepare_topic(llm, topic, chapter, rules, email, log, window, plan, frame, known, limit)
    rated = rank(llm, topic, prep["plan"].get("pico", ""), prep["new"], prep["period"]) if prep["new"] else {}
    return finish_topic(prep, rated, n_max)


def prepare_topic(llm: LLM, topic: dict, chapter: str, rules: dict, email: str, log=lambda msg: None,
                  window: list[str] | None = None, plan: dict | None = None, frame: dict | None = None,
                  known: dict | None = None, limit: int | None = None) -> dict:
    """Erster Teil von recommend_for_topic: Suchplan, PubMed-Suche und Kandidaten – alles bis vor die
    Bewertung. prep["new"] sind die noch zu bewertenden Kandidaten (siehe rank_jobs), finish_topic schließt ab."""
    fixed = manual_window(window)
    von, bis = fixed or time_window(rules.get("recency_months", 0))
    if plan is None:
        log(f"Suchstrategie für „{topic['title']}“ wird entwickelt…")
        plan = plan_search(llm, topic, chapter, von, bis, frame)
    plan = with_manual(plan, topic.get("manual_query", ""))
    log(f"PubMed-Suche für „{topic['title']}“…")
    pmids, stats = collect_candidates(plan, von, bis, email, limit=HUGE_CANDIDATES)
    limit = limit or candidate_limit(stats)  # Trefferzahlen entscheiden, ob das Thema breit ist
    evidence_hits = None
    if limit > BROAD_CANDIDATES:
        pmids, evidence_hits = evidence_first(plan, pmids, von, bis, email, limit)
    pmids = pmids[:limit]
    attempts = [{"window": [von.isoformat(), bis.isoformat()], "queries": plan.get("queries", []), "hits": stats}]
    if len(pmids) < MIN_CANDIDATES:
        # Zu wenige Treffer: zuerst sensitivere Suchstrategien, dann ein doppelt so langer Zeitraum
        log(f"Wenige Treffer für „{topic['title']}“ – Suche wird erweitert…")
        plan2 = broaden_search(llm, topic, plan)
        more, stats2 = collect_candidates(plan2, von, bis, email)
        attempts.append({"window": [von.isoformat(), bis.isoformat()], "queries": plan2.get("queries", []),
                         "hits": stats2})
        pmids = list(dict.fromkeys(pmids + more))
        if len(pmids) < MIN_CANDIDATES and not fixed:  # ein manueller Zeitraum wird nicht verlängert
            von2 = bis - (bis - von) * 2
            more, stats3 = collect_candidates(plan2, von2, bis, email)
            attempts.append({"window": [von2.isoformat(), bis.isoformat()], "queries": plan2.get("queries", []),
                             "hits": stats3})
            pmids = list(dict.fromkeys(pmids + more))
            if more:
                von = von2
        plan = {**plan, "queries": plan.get("queries", []) + plan2.get("queries", [])}
        stats = {f"{k} (erweitert)": v for k, v in stats2.items()} | stats
    records = pubmed.fetch_records(pmids, email) if pmids else {}
    candidates = prefilter([records[p] for p in pmids if p in records])
    log(f"{len(candidates)} Kandidaten für „{topic['title']}“ werden bewertet…")
    months = rules.get("recency_months") or 12
    period = (f"von {von.strftime('%m/%Y')} bis {bis.strftime('%m/%Y')}" if fixed else f"der letzten {months} Monate")
    known = known or {}
    new = [c for c in candidates if c["pmid"] not in known]
    if known:
        log(f"{len(candidates) - len(new)} Kandidaten für „{topic['title']}“ sind schon bewertet, {len(new)} neu.")
    return {"limit": limit, "evidence_hits": evidence_hits, "plan": plan, "window": [von.isoformat(), bis.isoformat()], "stats": stats,
            "attempts": attempts, "records": records, "n_candidates": len(candidates), "known": known, "new": new,
            "period": period}


def finish_topic(prep: dict, rated: dict[str, dict], n_max: int) -> dict:
    """Zweiter Teil von recommend_for_topic: Bewertungen (rated) mit den schon bekannten zusammenführen und
    die Auswahl treffen."""
    ranking = dict(prep["known"])
    ranking.update(rated)
    chosen, backup = select(ranking, n_max)
    records = prep["records"]
    return {
        "cand_limit": prep["limit"], "evidence_hits": prep.get("evidence_hits"),
        "plan": prep["plan"], "window": prep["window"], "query_hits": prep["stats"], "attempts": prep["attempts"],
        "n_candidates": prep["n_candidates"], "ranking": ranking, "recommended": chosen, "reserve": backup,
        "records": {p: records[p] for p in set(chosen) | set(backup) | set(ranking) if p in records},
    }
