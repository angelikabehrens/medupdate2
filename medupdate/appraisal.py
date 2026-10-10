"""Kritische Würdigung, Kürzung, Übersetzung, Stilanpassung und State of the Art.
Prompts und Nachbearbeitung aus MyLuo, API-Aufrufe über llm.LLM."""

from __future__ import annotations

import re

from . import prompts as M
from . import prompts_new as P
from .citations import (
    count_sentences,
    extract_intext_citation_numbers,
    extract_literature_list_numbers,
    finalize_sota_citations,
    renumber_and_prune_citations,
    split_sentences,
    split_sota_response,
    _CITE_RE,
)
from .config import MODEL_FAST, MODEL_MAIN
from .llm import LLM

# Längenstufen nach dem Skill "studien-wuerdigung" (Seitenäquivalent für die Umfangsplanung)
TIER_PAGES = {"Kurznachricht": 0.2, "½ Seite (Kurz)": 0.5, "1 Seite": 1.0, "1,5 Seiten (Lang)": 1.5,
              "2 Seiten": 2.0}
TIER_ORDER = ["Kurznachricht", "½ Seite (Kurz)", "1 Seite", "1,5 Seiten (Lang)", "2 Seiten"]


def background_block(docs: list[tuple[str, str]]) -> str:
    """docs: [(Bezeichnung, Text)] – Leitlinien-Auszüge und weitere Hintergrundliteratur."""
    if not docs:
        return ""
    parts = [f"--- {name} ---\n{text}" for name, text in docs]
    return (
        "\n\nAktuelle Leitlinien und Hintergrundliteratur zu diesem Thema (von der App recherchiert oder "
        "vom Autor bereitgestellt) – nutze dies für Abschnitt 4 und 5 VORRANGIG vor der eigenen Websuche, "
        "sofern einschlägig, da dies die maßgeblichen Quellen sind. Ergänze die Websuche zusätzlich dort, "
        "wo diese Dokumente die Fragestellung nicht abdecken. Diese Dokumente gelten als zitierfähige "
        "Quellen wie Websuche-Ergebnisse auch; zitiere sie mit ihrem vollständigen Titel, der "
        "herausgebenden Fachgesellschaft und dem Jahr:\n\n" + "\n\n".join(parts) + "\n"
    )


def fix_citation_consistency(llm: LLM, text: str, sprache_kw: str) -> str:
    cited = extract_intext_citation_numbers(text)
    listed = extract_literature_list_numbers(text)
    if cited == listed:
        return text
    lit_label = "References" if sprache_kw == "Englisch" else "Literatur"
    sprache_instr = ("Antworte auf Englisch, in derselben Sprache wie der Ausgangstext." if sprache_kw == "Englisch"
                     else "Antworte auf Deutsch, in derselben Sprache wie der Ausgangstext.")
    try:
        fixed = llm.text(P.citation_fix_prompt(text, lit_label, sorted(listed), sorted(cited), sprache_instr),
                         model=MODEL_FAST, max_tokens=24000, effort="low")
        # Schutz vor Textverlust: eine Korrektur, die deutlich kürzer ist, wird verworfen
        return fixed if fixed and len(fixed) >= 0.85 * len(text) else text
    except Exception:
        return text


def generate_appraisal(llm: LLM, entry: dict, docs: list[tuple[str, str]], sprache_kw: str = "Englisch") -> str:
    """entry: title, journal, pubdate, pub_types, abstract, full_text (optional)."""
    fulltext_block = (f"\nVolltext (Auszug, automatisch geladen oder hochgeladen):\n{entry['full_text']}\n"
                      if entry.get("full_text") else "")
    prompt = M.appraisal_prompt(entry, sprache_kw, fulltext_block, background_block(docs), max_websuchen=10)
    text = llm.text(prompt, model=MODEL_MAIN, web_search=10, max_tokens=32000, effort="high")
    text = fix_citation_consistency(llm, text, sprache_kw)
    return renumber_and_prune_citations(text)


def translate_appraisal(llm: LLM, text: str, target_lang: str) -> str:
    out = llm.text(M.translate_appraisal_prompt(text, target_lang), model=MODEL_FAST, max_tokens=24000,
                   effort="low")
    return renumber_and_prune_citations(out)


def kurznachricht(llm: LLM, full_text: str, sprache_kw: str, abstract_text: str = "") -> str:
    prompt = M.kurznachricht_prompt(full_text, sprache_kw, abstract_text)

    def _clean(t: str) -> str:
        t = re.sub(r"\*\*|^#+\s*", "", t, flags=re.MULTILINE)
        t = _CITE_RE.sub("", t)
        t = " ".join(t.split())
        return re.sub(r"\s+([.,;:])", r"\1", t)

    text = _clean(llm.text(prompt, model=MODEL_FAST, max_tokens=3000, effort="medium"))
    n = count_sentences(text)
    if n > 3:
        try:
            retry = _clean(llm.text(
                f"Das sind {n} Sätze – erlaubt sind höchstens 3. Kürze auf höchstens 3 bündige Sätze "
                "(Satz 1: Ziel, Design, Patientenzahl; danach nur die Hauptaussage) und behalte dabei alle "
                "Zahlenangaben unverändert bei. Gib nur den fertigen Text aus.",
                history=[{"role": "user", "content": prompt}, {"role": "assistant", "content": text}],
                model=MODEL_FAST, max_tokens=3000, effort="medium"))
            if retry and count_sentences(retry) <= n:
                text = retry
        except Exception:
            pass
    return text


def shorten(llm: LLM, full_text: str, tier: str, sprache_kw: str, abstract_text: str = "") -> str:
    """Kürzung nach dem Skill "studien-wuerdigung" (Längenstufen siehe prompts.SKILL_LAENGEN_STUFEN)."""
    if tier == "Kurznachricht":
        return kurznachricht(llm, full_text, sprache_kw, abstract_text)
    text = llm.text(M.skill_shorten_prompt(full_text, tier, sprache_kw), model=MODEL_MAIN, max_tokens=8000,
                    effort="medium")
    if not text:
        raise ValueError("Die KI hat für die Kürzung keine (leere) Antwort geliefert.")
    return renumber_and_prune_citations(text)


def translate_structured(llm: LLM, text: str) -> str:
    return llm.text(P.translate_structured_prompt(text), model=MODEL_FAST, max_tokens=8000, effort="low")


def translate_abstract(llm: LLM, text: str) -> str:
    return llm.text(M.translate_abstract_prompt(text), model=MODEL_FAST, max_tokens=6000, effort="low")


# ---------- Stilanpassung an frühere Manuskripte des Autors ----------

def _numbers(t: str) -> list[str]:
    return sorted(re.findall(r"\d+(?:[.,]\d+)?", t))


def style_profile(llm: LLM, samples: list[str]) -> str:
    return llm.text(P.style_profile_prompt([s[:8000] for s in samples[:3]]), model=MODEL_MAIN, max_tokens=4000,
                    effort="medium")


def apply_style(llm: LLM, text: str, profile: str, sample_excerpt: str) -> tuple[str, bool]:
    """Gibt (Text, angewendet) zurück. Die Stilfassung wird verworfen, wenn sich Zahlen oder
    Klammerverweise geändert haben oder die Länge stark abweicht – dann bleibt das Original."""
    try:
        out = llm.text(P.style_apply_prompt(text, profile, sample_excerpt[:2500]), model=MODEL_MAIN,
                       max_tokens=8000, effort="medium")
    except Exception:
        return text, False
    if not out or _numbers(out) != _numbers(text):
        return text, False
    if not (0.8 * len(text) <= len(out) <= 1.25 * len(text) + 40):
        return text, False
    return out, True


# ---------- State of the Art ----------

def generate_sota(llm: LLM, thema_text: str, sprache_kw: str, guideline_docs: list[tuple[str, str]]) -> dict:
    prompt = M.sota_prompt(thema_text, sprache_kw)
    if guideline_docs:
        prompt += (
            "\n\nBereits recherchierte aktuelle Leitlinien zu diesem Thema (Auszüge). Nutze sie vorrangig als "
            "Baseline-Evidenz und zitiere sie wie gefundene Quellen:\n\n"
            + "\n\n".join(f"--- {n} ---\n{t[:6000]}" for n, t in guideline_docs)
        )
    raw = llm.text(prompt, model=MODEL_MAIN, web_search=6, max_tokens=6000, effort="high")
    body, entries = split_sota_response(raw)
    if not body:
        raise ValueError("Die KI hat für den State of the Art keine (leere) Antwort geliefert.")
    n_sent, n_words = count_sentences(body), len(body.split())
    lit_label = "References" if sprache_kw == "Englisch" else "Literatur"
    if n_sent > 5 or n_words > 130:
        try:
            retry_raw = llm.text(
                f"Der Fließtext hat {n_sent} Sätze mit {n_words} Wörtern – deutlich zu lang. Kürze auf 3 bis 4 "
                "bündige Sätze (höchstens 5), insgesamt höchstens ca. 100 Wörter, jeder Satz höchstens ca. 30 "
                "Wörter. Behalte nur das Wesentliche, erfinde nichts hinzu und behalte die Sprache bei. Behalte "
                "die Klammerverweise vor dem Satzpunkt und gib danach unverändert die Literaturliste unter der "
                f"Überschrift „{lit_label}“ aus (nur Quellen, die im gekürzten Text noch zitiert werden). Gib "
                "keine weitere Erläuterung aus.",
                history=[{"role": "user", "content": prompt}, {"role": "assistant", "content": raw}],
                model=MODEL_MAIN, max_tokens=4000, effort="medium")
            rb, re_entries = split_sota_response(retry_raw)
            if rb and (count_sentences(rb) < n_sent or len(rb.split()) < n_words):
                body, entries = rb, re_entries or entries
        except Exception:
            pass
    sentences = split_sentences(body)
    if len(sentences) > 5:
        body = " ".join(sentences[:5])
    body, literature = finalize_sota_citations(body, entries)
    return {"text": body, "literature": literature}


def translate_sota(llm: LLM, sota: dict) -> dict:
    """Übersetzt nur den Fließtext ins Deutsche; Verweise und Literaturliste bleiben gleich."""
    text = llm.text(
        "Übersetze den folgenden Absatz präzise und fachsprachlich ins Deutsche. Klammerverweise wie "
        "\"(1)\" bleiben exakt an ihrer Stelle, Zahlen unverändert. Gib nur die Übersetzung aus.\n\n"
        + sota["text"], model=MODEL_FAST, max_tokens=2000, effort="low")
    return {"text": " ".join(text.split()), "literature": list(sota["literature"])}


# ---------- Längenstufen automatisch festlegen ----------

def tier_for(length_score: float) -> str:
    if length_score >= 80:
        return "1,5 Seiten (Lang)"
    if length_score >= 65:
        return "1 Seite"
    return "½ Seite (Kurz)"


def estimate_pages(tiers: dict[str, str], n_sections: int) -> float:
    """Grobe Schätzung: Besprechungen + Titelseite + State of the Art je Abschnitt + Literatur."""
    overhead = 1.0 + 0.35 * n_sections + 0.16 * len(tiers)
    return round(sum(TIER_PAGES[t] for t in tiers.values()) + overhead, 1)


def plan_tiers(pubs: list[dict], rules: dict, n_sections: int) -> tuple[dict[str, str], list[str]]:
    """pubs: [{"id", "length_score"}]. Die Länge richtet sich nach dem Längenwert (Studienqualität vor
    Bedeutung der Fragestellung vor Impact Factor): ≥80 → 1,5 Seiten, ≥65 → 1 Seite, sonst ½ Seite.
    Liegt ein Seitenlimit vor, werden zuerst die Arbeiten mit dem niedrigsten Längenwert stufenweise
    gekürzt (nicht unter ½ Seite). Reicht das nicht, werden die schwächsten Arbeiten als "overflow"
    zurückgegeben. Rückgabe: (Längenstufen, overflow-IDs)."""
    tiers = {p["id"]: tier_for(p.get("length_score", 0)) for p in pubs}
    max_pages = (rules or {}).get("max_pages") or 0
    if not max_pages:
        return tiers, []
    order = sorted(pubs, key=lambda p: p.get("length_score", 0))
    while estimate_pages(tiers, n_sections) > max_pages:
        for p in order:
            idx = TIER_ORDER.index(tiers[p["id"]])
            if idx > 1:
                tiers[p["id"]] = TIER_ORDER[idx - 1]
                break
        else:
            break
    overflow = []
    for p in order:
        if estimate_pages(tiers, n_sections) <= max_pages or len(tiers) <= 1:
            break
        tiers.pop(p["id"])
        overflow.append(p["id"])
    return tiers, overflow


def sota_topics(topic_order: list[str], topics: dict[str, dict], pub_count: dict[str, int]) -> list[str]:
    """Je Hauptabschnitt genau ein State of the Art, und zwar vor dem Thema mit den meisten
    aufgenommenen Publikationen (bei Gleichstand das erste im Inhaltsverzeichnis)."""
    best: dict[str, str] = {}
    for tid in topic_order:
        if not pub_count.get(tid):
            continue
        sec = topics[tid].get("section_number") or topics[tid].get("section_title") or tid
        if sec not in best or pub_count[tid] > pub_count[best[sec]]:
            best[sec] = tid
    return [t for t in topic_order if t in best.values()]
