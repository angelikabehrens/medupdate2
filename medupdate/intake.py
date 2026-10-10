"""Schritt 1: Inhaltsverzeichnis, Hinweise für Autoren und Formatvorlage auswerten."""

from __future__ import annotations

import io

from docx import Document

from . import prompts_new as P
from .llm import INT, LLM, STR, arr, obj

TOC_SCHEMA = obj({
    "chapter_number": STR,
    "chapter_title": STR,
    "topics": arr(obj({"number": STR, "title": STR, "english_search_focus": STR,
                        "section_number": STR, "section_title": STR})),
})

RULES_SCHEMA = obj({
    "citation_style": STR,
    "citation_examples": STR,
    "intext_citation_style": STR,
    "recency_months": INT,
    "max_references": INT,
    "max_pages": INT,
    "min_figures": INT,
    "max_figures": INT,
    "pubs_per_topic": INT,
    "length_rules": STR,
    "structure_rules": STR,
    "language_rules": STR,
    "figure_rules": STR,
    "other_rules": arr(STR),
})


def parse_toc(llm: LLM, toc_text: str) -> dict:
    data = llm.json(P.toc_prompt(toc_text[:40000]), TOC_SCHEMA, max_tokens=8000)
    for i, t in enumerate(data.get("topics", []), start=1):
        t["id"] = f"T{i}"
        if not (t.get("section_number") or t.get("section_title")):
            t["section_number"], t["section_title"] = t.get("number", "") or t["id"], t["title"]
    return data


def parse_author_rules(llm: LLM, guide_text: str) -> dict:
    return llm.json(P.author_rules_prompt(guide_text[:80000]), RULES_SCHEMA, max_tokens=8000)


def inspect_template(template_bytes: bytes) -> dict:
    """Welche Formatvorlagen bringt die Manuskriptvorlage mit? (für die Übernahme in Schritt 2)"""
    doc = Document(io.BytesIO(template_bytes))
    names = sorted({s.name for s in doc.styles if s.name})
    known = ["MainTitle1", "StandardTitle1", "StandardTitle2", "Author", "Biblio", "BiblioTitle1",
             "ImageSubtitle", "Toc1", "Toc2"]
    return {"style_names": names, "med_update_styles": [k for k in known if k in names],
            "paragraphs": len(doc.paragraphs)}
