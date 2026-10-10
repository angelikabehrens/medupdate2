"""Gesamtmanuskript als Word-Datei. Aufbau wie in MyLuo (Überschrift 2 je Publikation, lokale
Literaturlisten, "State of the Art" vor der ersten Publikation eines Themas), damit die
Literatur-Zusammenführung und die Übertragung in die Formatvorlage unverändert funktionieren."""

from __future__ import annotations

import io
import re
from datetime import date

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

from .docx_tools import add_field, add_floating_picture, render_line_with_bold_label, style_document


def short_citation(rec: dict, lit_numbers: list[int]) -> str:
    """'Erstautor et al., Journal Jahr (N)' – wie format_short_citation in MyLuo."""
    authors = rec.get("authors") or []
    first = authors[0].split(" ")[0] if authors else "?"
    nums = ",".join(str(n) for n in lit_numbers)
    return f"{first} et al., {rec.get('journal', '')} {rec.get('year', '')} ({nums})".strip()


def build_docx(title: str, blocks: list[dict], lang: str) -> bytes:
    """blocks: [{"topic": str, "number": str (optional, z.B. "8.1.2"), "section": (Nummer, Titel) oder None
                 (nur beim ersten Thema eines neuen Hauptabschnitts), "sota": {"text", "literature"} | None,
                 "pubs": [{"title", "rec", "lines", "abstract", "literature", "figure"}]}]
    Mit Nummern entsteht auf Seite 1 ein Inhaltsverzeichnis (Hauptabschnitte und Themen).
    lang: "Deutsch" oder "Englisch" (für Labels)."""
    lit_label = "References" if lang == "Englisch" else "Literatur"
    doc = Document()
    style_document(doc)

    p = doc.add_paragraph()
    r = p.add_run(title or "Gesamtmanuskript")
    r.bold = True
    r.font.size = Pt(14)
    doc.add_paragraph(date.today().strftime("%d.%m.%Y"))
    doc.add_paragraph("")
    toc_lines = []
    for b in blocks:
        if b.get("section"):
            toc_lines.append((1, *b["section"]))
        if b.get("number"):
            toc_lines.append((2, b["number"], b["topic"]))
    if toc_lines:
        p = doc.add_paragraph()
        p.add_run("Inhaltsverzeichnis" if lang != "Englisch" else "Contents").bold = True
        for level, num, text in toc_lines:
            tp = doc.add_paragraph(f"{num}\t{text}")
            if level == 2:
                tp.paragraph_format.left_indent = Pt(18)
        doc.add_paragraph("")
    p = doc.add_paragraph()
    p.add_run("Suchbegriffe").bold = True
    for b in blocks:
        tp = doc.add_paragraph(f"- {b['topic']}")
        tp.paragraph_format.left_indent = Pt(18)
    doc.add_paragraph("")
    doc.add_paragraph("")
    p = doc.add_paragraph()
    p.add_run("Interessenskonflikte").bold = True
    doc.add_paragraph("")
    doc.add_paragraph("")
    doc.add_page_break()

    img_n = tab_n = 0
    for b in blocks:
        if b.get("section"):
            sp = doc.add_paragraph()
            sp.add_run(f"Abschnitt: „{b['section'][0]}\t{b['section'][1]}“").bold = True
        if b.get("number"):
            mp = doc.add_paragraph()
            mp.add_run(f"Thema: „{b['number']}\t{b['topic']}“").bold = True
        elif not b.get("section"):
            mp = doc.add_paragraph()
            mp.add_run(f"Thema: „{b['topic']}“").bold = True

        sota = b.get("sota")
        if sota and sota.get("text"):
            hp = doc.add_paragraph()
            hp.add_run("State of the Art").bold = True
            doc.add_paragraph(sota["text"].strip())
            if sota.get("literature"):
                doc.add_paragraph("")
                lp = doc.add_paragraph()
                lp.add_run(lit_label).bold = True
                for i, e in enumerate(sota["literature"], start=1):
                    doc.add_paragraph(f"{i}. {e}")
            doc.add_paragraph("")

        for pub in b["pubs"]:
            doc.add_heading(pub["title"], level=2)
            doc.add_paragraph(short_citation(pub["rec"], [1]))
            if pub.get("abstract"):
                doc.add_paragraph(pub["abstract"])
            for line in pub["lines"]:
                line = re.sub(r"^#+\s*", "", line.strip())
                if line and not re.fullmatch(r"[-*_—―]{3,}", line):
                    render_line_with_bold_label(doc, line)
            if pub.get("literature"):
                doc.add_paragraph("")
                lp = doc.add_paragraph()
                lp.add_run(lit_label).bold = True
                for i, e in enumerate(pub["literature"], start=1):
                    doc.add_paragraph(f"{i}. {e}")
            fig = pub.get("figure")
            if fig and fig.get("bytes"):
                try:
                    ip = doc.add_paragraph()
                    add_floating_picture(ip, io.BytesIO(fig["bytes"]), Inches(5.5))
                    if fig.get("typ") == "Tabelle":
                        tab_n += 1
                        prefix = f"Tab. {tab_n}"
                    else:
                        img_n += 1
                        prefix = f"Abb. {img_n}"
                    authors = pub["rec"].get("authors") or ["?"]
                    ref = f"{authors[0]} et al., {pub['rec'].get('journal', '')}. {pub['rec'].get('year', '')}"
                    cp = doc.add_paragraph()
                    cp.add_run(f"{prefix}: {fig.get('caption', '').rstrip('.')}. {ref} (1)").italic = True
                except Exception:
                    pass
            doc.add_paragraph("")

    footer = doc.sections[0].footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    add_field(footer, "PAGE")
    footer.add_run("-")
    add_field(footer, "NUMPAGES")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_recommendation_docx(chapter: str, topics: list[dict], records: dict, rankings: dict) -> bytes:
    """Liste der empfohlenen Publikationen (zum Beschaffen der Volltexte)."""
    doc = Document()
    style_document(doc)
    h = doc.add_paragraph()
    rr = h.add_run(f"Literaturempfehlung – {chapter}" if chapter else "Literaturempfehlung")
    rr.bold = True
    rr.font.size = Pt(14)
    doc.add_paragraph(date.today().strftime("%d.%m.%Y"))
    for t in topics:
        doc.add_heading(t["title"], level=2)
        for n, pmid in enumerate(t.get("recommended", []), start=1):
            rec = records.get(pmid, {})
            it = rankings.get(pmid, {})
            authors = rec.get("authors") or []
            a = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
            p = doc.add_paragraph()
            p.add_run(f"{n}. {a}. ")
            p.add_run(rec.get("title", "")).bold = True
            p.add_run(f" {rec.get('journal', '')} {rec.get('year', '')}. PMID: {pmid}")
            if rec.get("doi"):
                p.add_run(f" · doi:{rec['doi']}")
            if rec.get("is_oa"):
                p.add_run(" [Open Access]").italic = True
            if it:
                q = doc.add_paragraph()
                q.add_run(f"Priorität {it.get('priority', '?')} · Qualität {it.get('quality', '?')} · "
                          f"Bedeutung {it.get('question_importance', '?')} · "
                          f"IF {it.get('journal_impact_factor') or 'k. A.'} · "
                          f"{it.get('study_design', '')}. {it.get('rationale', '')}").italic = True
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
