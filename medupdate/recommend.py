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

import math

from datetime import date, timedelta

from . import prompts_new as P
from . import pubmed
from .config import MAX_CANDIDATES_PER_TOPIC, MODEL_MAIN
from .llm import BOOL, INT, LLM, NUM, STR, arr, enum, obj

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


def plan_search(llm: LLM, topic: dict, chapter: str, von: date, bis: date) -> dict:
    return llm.json(
        P.search_plan_prompt(topic["title"], topic.get("english_search_focus", ""), chapter,
                             von.strftime("%d.%m.%Y"), bis.strftime("%d.%m.%Y")),
        PLAN_SCHEMA, model=MODEL_MAIN, max_tokens=12000,
    )


MIN_CANDIDATES = 8


def broaden_search(llm: LLM, topic: dict, plan: dict) -> dict:
    failed = "\n".join(f"- {q['name']}: {q['query']}" for q in plan.get("queries", []))
    data = llm.json(P.search_broaden_prompt(topic["title"], topic.get("english_search_focus", ""), failed),
                    PLAN_SCHEMA, model=MODEL_MAIN, max_tokens=12000)
    data["pico"] = plan.get("pico") or data.get("pico", "")
    return data


def collect_candidates(plan: dict, von: date, bis: date, email: str,
                       limit: int = MAX_CANDIDATES_PER_TOPIC) -> tuple[list[str], dict]:
    """Führt alle Suchstrategien aus und mischt die Treffer im Reißverschlussverfahren."""
    suffix = f" AND {pubmed.date_filter(von, bis)} AND (english[la] OR german[la]) AND hasabstract"
    per_query: list[list[str]] = []
    stats = {}
    for q in plan.get("queries", []):
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


def rank(llm: LLM, topic: dict, pico: str, records: list[dict], recency_text: str,
         batch_size: int = 15) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        block = "\n".join(
            f"---\nPMID: {r['pmid']}\nTitel: {r['title']}\nJournal: {r['journal']} ({r['year']})\n"
            f"Publication Type (PubMed): {', '.join(r['pub_types'])}\nAbstract: {r['abstract'][:3500]}"
            for r in batch
        )
        data = llm.json(P.ranking_prompt(topic["title"], pico, block, recency_text), RANK_SCHEMA,
                        model=MODEL_MAIN, max_tokens=32000)
        valid = {r["pmid"] for r in batch}
        for item in data.get("items", []):
            if item.get("pmid") in valid:
                result[item["pmid"]] = item
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
    """Nur Priorität A; höchstens 3 Arbeiten pro Fragestellung (gemeinsame Würdigung) und höchstens n_max je Thema.
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


def recommend_for_topic(llm: LLM, topic: dict, chapter: str, rules: dict, email: str,
                        n_max: int, log=lambda msg: None, window: list[str] | None = None) -> dict:
    fixed = manual_window(window)
    von, bis = fixed or time_window(rules.get("recency_months", 0))
    log(f"Suchstrategie für „{topic['title']}“ wird entwickelt…")
    plan = plan_search(llm, topic, chapter, von, bis)
    log(f"PubMed-Suche für „{topic['title']}“…")
    pmids, stats = collect_candidates(plan, von, bis, email)
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
    ranking = rank(llm, topic, plan.get("pico", ""), candidates, period) if candidates else {}
    chosen, backup = select(ranking, n_max)
    return {
        "plan": plan, "window": [von.isoformat(), bis.isoformat()], "query_hits": stats, "attempts": attempts,
        "n_candidates": len(candidates), "ranking": ranking, "recommended": chosen, "reserve": backup,
        "records": {p: records[p] for p in set(chosen) | set(backup) | set(ranking) if p in records},
    }
