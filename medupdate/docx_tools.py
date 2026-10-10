"""Word-Hilfsfunktionen aus MyLuo: Grundformatierung, Felder, Labels, schwebende Bilder,
Übertragung in die Manuskriptvorlage und Prüfprotokoll."""

from __future__ import annotations

import io
import re
from datetime import date

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


def style_document(doc: Document) -> None:
    """Setzt die gewünschte Grundformatierung: Arial 11, Zeilenabstand 1,5,
    Überschriften schwarz statt blau."""
    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    for heading_name in ("Heading 1", "Heading 2"):
        style = doc.styles[heading_name]
        style.font.name = "Arial"
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT


def add_field(paragraph, field_code: str):
    """Fügt ein Word-Feld ein (z.B. PAGE oder NUMPAGES), das sich beim Öffnen
    automatisch berechnet."""
    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr_text = OxmlElement("w:instrText")
    instr_text.set(qn("xml:space"), "preserve")
    instr_text.text = field_code
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr_text)
    run._r.append(fld_end)
    return run


_SKILL_BOLD_LABELS = [
    "Studie:", "Ergebnisse:", "Schlussfolgerung der Autoren:", "Kommentar:", "Fazit für die Praxis:",
    "Study:", "Results:", "Authors' Conclusion:", "Comment:", "Conclusion for Practice:",
    # Verknüpfungs-Format (Schritt 4B: gemeinsame Würdigung mehrerer Publikationen)
    "Übersicht der Publikationen", "Publikation 1:", "Publikation 2:", "Publikation 3:",
    "Einordnung der Verknüpfung", "Kritische Würdigung", "Gemeinsames Fazit",
    "Overview of the Publications", "Publication 1:", "Publication 2:", "Publication 3:",
    "Assessment of the Linkage", "Critical Appraisal", "Joint Conclusion",
]


def render_line_with_bold_label(doc: Document, line: str) -> None:
    """Fügt eine Zeile der Schritt-7-Kürzung ein und unterstreicht ein bekanntes Label
    (z.B. 'Fazit für die Praxis:') am Zeilenanfang, falls vorhanden – der Rest der
    Zeile bleibt normal formatiert. Entfernt dabei auch übrig gebliebene Markdown-
    Sternchen ('**') irgendwo in der Zeile, nicht nur am Rand."""
    clean_line = re.sub(r"\*\*", "", line).strip("* ").strip()
    matched_label = None
    for label in _SKILL_BOLD_LABELS:
        if clean_line.lower().startswith(label.lower()):
            matched_label = clean_line[: len(label)]
            break
    p = doc.add_paragraph()
    if matched_label:
        p.add_run(matched_label).underline = True
        p.add_run(clean_line[len(matched_label):])
    else:
        p.add_run(clean_line)


# Zitatmarker im Fließtext: "(3)", "(2,4)" oder "[3]" – aber NICHT direkt hinter einem


def format_short_citation(first_author: str, journal: str, pubdate: str, lit_numbers: list[int]) -> str:
    """Baut die kompakte Zitierzeile unter dem Publikationstitel:
    'Erstautor et al., Journal Jahr (N)' bzw. '(N,M,...)' bei mehreren zugehörigen
    Literaturnummern."""
    year_match = re.search(r"(19|20)\d{2}", pubdate or "")
    year = year_match.group(0) if year_match else (pubdate or "").strip()
    numbers_text = ",".join(str(n) for n in lit_numbers)
    return f"{first_author} et al., {journal} {year} ({numbers_text})".strip()


def parse_citation_parts(citation_text: str) -> tuple[str, str, str]:
    """Extrahiert grob Erstautor, Journal und Jahr aus einer vorhandenen, nicht
    strukturiert vorliegenden Zitierzeile – Fallback für hochgeladene Dateien, deren
    Angaben wir nicht selbst in Autor/Journal/Jahr-Felder aufgeteilt haben."""
    citation_text = (citation_text or "").strip()
    first_author = re.split(r",| et al", citation_text, maxsplit=1)[0].strip()
    year_match = re.search(r"(19|20)\d{2}", citation_text)
    year = year_match.group(0) if year_match else ""
    journal = ""
    m = re.search(r"\.\s*([A-Z][A-Za-z0-9 &\-]+?)\.\s*(?:19|20)\d{2}", citation_text)
    if m:
        journal = m.group(1).strip()
    return first_author, journal, year


def add_floating_picture(paragraph, image_stream, width_emu: int) -> None:
    """Fügt ein Bild als frei mit der Maus verschiebbares (schwebendes) Bild mit
    Textumfluss ein, statt es fest im Textfluss zu verankern. python-docx unterstützt
    das nicht nativ (nur "inline"-Bilder) – die zugrunde liegende Word-XML-Struktur
    wird hier von <wp:inline> auf <wp:anchor> umgebaut, mit erlaubtem Verschieben
    (allowOverlap, nicht gesperrt) und quadratischem Textumfluss."""
    run = paragraph.add_run()
    run.add_picture(image_stream, width=width_emu)

    drawing = run._element.find(qn("w:drawing"))
    inline = drawing.find(qn("wp:inline"))

    extent = inline.find(qn("wp:extent"))
    doc_pr = inline.find(qn("wp:docPr"))
    graphic_frame_pr = inline.find(qn("wp:cNvGraphicFramePr"))
    graphic = inline.find(qn("a:graphic"))

    drawing.remove(inline)

    anchor = OxmlElement("wp:anchor")
    anchor.set("distT", "114300")
    anchor.set("distB", "114300")
    anchor.set("distL", "114300")
    anchor.set("distR", "114300")
    anchor.set("simplePos", "0")
    anchor.set("relativeHeight", "251659264")
    anchor.set("behindDoc", "0")
    anchor.set("locked", "0")
    anchor.set("layoutInCell", "1")
    anchor.set("allowOverlap", "1")

    simple_pos = OxmlElement("wp:simplePos")
    simple_pos.set("x", "0")
    simple_pos.set("y", "0")
    anchor.append(simple_pos)

    position_h = OxmlElement("wp:positionH")
    position_h.set("relativeFrom", "column")
    align_h = OxmlElement("wp:align")
    align_h.text = "center"
    position_h.append(align_h)
    anchor.append(position_h)

    position_v = OxmlElement("wp:positionV")
    position_v.set("relativeFrom", "paragraph")
    pos_offset_v = OxmlElement("wp:posOffset")
    pos_offset_v.text = "0"
    position_v.append(pos_offset_v)
    anchor.append(position_v)

    anchor.append(extent)

    wrap_square = OxmlElement("wp:wrapSquare")
    wrap_square.set("wrapText", "bothSides")
    anchor.append(wrap_square)

    anchor.append(doc_pr)
    if graphic_frame_pr is not None:
        anchor.append(graphic_frame_pr)
    anchor.append(graphic)

    drawing.append(anchor)


def _source_toc_entries(source_doc: Document) -> list[tuple[str, str]]:
    """Liest das Inhaltsverzeichnis der Titelseite des Rohmanuskripts: [(Nummer, Titel)]."""
    paras = [p.text.strip() for p in source_doc.paragraphs]
    try:
        start = next(i for i, t in enumerate(paras) if t in ("Inhaltsverzeichnis", "Contents"))
    except StopIteration:
        return []
    out = []
    for t in paras[start + 1:]:
        m = re.match(r"^(\d+(?:\.\d+)*)\t(.+)$", t)
        if not m:
            if out or t:
                break
            continue
        out.append((m.group(1), m.group(2)))
    return out


def copy_document_content_into_template(
    source_doc: Document, template_bytes: bytes, search_terms: list[str] | None = None
) -> bytes:
    """Kopiert den Publikationsinhalt (Text mit Formatierung, schwebende Bilder) aus
    dem fertigen, korrigierten Manuskript in eine vom Nutzer bereitgestellte eigene
    Manuskriptvorlage (z.B. im "studien-wuerdigung"-Skill-Stil mit Formatvorlagen
    "StandardTitle2", "Author", "BiblioTitle1", "Biblio", "ImageSubtitle").

    - Datum und Suchbegriffe werden zwischen dem Vorlagentitel/-Inhaltsverzeichnis und
      der (in der Vorlage bereits vorhandenen) Überschrift "Interessenkonflikte"/
      "Interessenskonflikte" eingefügt; unsere eigene Standard-Titelseite
      ("Gesamtmanuskript", eigene Interessenskonflikte-Überschrift) wird dafür NICHT
      übernommen, da die Vorlage das bereits mitbringt.
    - Beispiel-/Platzhalterinhalt der Vorlage (jede Zeile, die den Namen ihrer eigenen
      Formatvorlage im Text nennt, z.B. "8.1.1 StandardTitle2", und alles danach) wird
      entfernt.
    - Publikationstitel erhalten die Formatvorlage "StandardTitle2" (fett, ohne
      Abstand zur folgenden Zitierzeile); die Zitierzeile erhält die Formatvorlage
      "Author" (ebenfalls ohne Abstand nach oben). Die Literaturziffer am Ende der
      Zitierzeile wird entfernt und stattdessen an das Ende des ersten Satzes des
      folgenden Fließtexts gesetzt (Literaturstellen im Text statt hinter dem Titel).
    - Der abschließende "Literatur"-Abschnitt erhält die Formatvorlagen
      "BiblioTitle1"/"Biblio", Bildunterschriften "ImageSubtitle", sofern in der
      Vorlage vorhanden.
    """
    template_doc = Document(io.BytesIO(template_bytes))
    template_paragraphs = list(template_doc.paragraphs)
    template_style_names = {s.name for s in template_doc.styles}

    # Kapitelnummer aus der Haupttitel-Formatvorlage extrahieren (z.B. "8" aus
    # "8\tendoskopie: oberer gi-trakt und ercp"), damit Publikationstitel dezimal
    # nummeriert werden können ("8.1", "8.2", ...) – die Formatvorlage "StandardTitle2"
    # hat einen hängenden Einzug, der genau für dieses "Nummer<TAB>Titel"-Muster gedacht ist.
    chapter_number = None
    for p in template_paragraphs:
        if p.style.name == "MainTitle1":
            m = re.match(r"^(\d+)", p.text.strip())
            if m:
                chapter_number = m.group(1)
            break

    # 1) Überschrift "Interessenkonflikte"/"Interessenskonflikte" in der Vorlage finden
    ik_idx = None
    for i, p in enumerate(template_paragraphs):
        if p.text.strip().lower() in ("interessenkonflikte", "interessenskonflikte"):
            ik_idx = i
            break

    # 2) Beispiel-/Platzhalterinhalt NACH diesem Block finden: die erste Zeile, die den
    # Namen ihrer eigenen Formatvorlage selbst im Text nennt (z.B. "StandardTitle1-1"),
    # ist zuverlässig der Beginn des reinen Anschauungsmaterials der Vorlage.
    search_from = ik_idx + 1 if ik_idx is not None else 0
    example_start_idx = None
    for i in range(search_from, len(template_paragraphs)):
        p = template_paragraphs[i]
        if p.style.name in p.text:
            example_start_idx = i
            break

    # 3) Diesen Beispielinhalt aus der Vorlage entfernen
    if example_start_idx is not None:
        for p in template_paragraphs[example_start_idx:]:
            el = p._element
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)

    # 3b) Inhaltsverzeichnis auf Seite 1: aus dem Quelldokument (bereits auf die tatsächlich
    # besprochenen Themen gekürzt und neu nummeriert). Ein Beispiel-Inhaltsverzeichnis der Vorlage
    # (Absätze mit Toc-Formatvorlagen vor "Interessenkonflikte") wird dabei ersetzt.
    toc_entries = _source_toc_entries(source_doc)
    if toc_entries:
        toc_styles = {1: "Toc1" if "Toc1" in template_style_names else None}
        toc_styles[2] = "Toc2" if "Toc2" in template_style_names else toc_styles[1]
        toc_styles[3] = "Toc3" if "Toc3" in template_style_names else toc_styles[2]
        anchor = None
        for p in template_paragraphs[: ik_idx if ik_idx is not None else len(template_paragraphs)]:
            if p._element.getparent() is None:
                continue
            if p.style.name.lower().startswith("toc") or p.text.strip().lower() in ("inhalt", "inhaltsverzeichnis"):
                if anchor is None:
                    anchor = template_doc.add_paragraph("")
                    p._element.addprevious(anchor._element)
                p._element.getparent().remove(p._element)
        if anchor is None:
            main = next((p for p in template_paragraphs if p.style.name == "MainTitle1"), None)
            anchor = template_doc.add_paragraph("")
            if main is not None:
                main._element.addnext(anchor._element)
            elif ik_idx is not None:
                template_paragraphs[ik_idx]._element.addprevious(anchor._element)
        min_depth = min(num.count(".") for num, _ in toc_entries)
        last = anchor._element
        head = template_doc.add_paragraph()
        head.add_run("Inhalt").bold = True
        last.addnext(head._element)
        last = head._element
        for num, text in toc_entries:
            level = min(3, num.count(".") - min_depth + 1)
            tp = template_doc.add_paragraph(f"{num}\t{text}", style=toc_styles[level])
            # ohne eigene Formatvorlage für die Ebene: Einrückung je Ebene
            if level > 1 and (toc_styles[level] is None or toc_styles[level] == toc_styles[level - 1]):
                tp.paragraph_format.left_indent = Pt(18 * (level - 1))
            last.addnext(tp._element)
            last = tp._element

    # 4) Datum + Suchbegriffe VOR "Interessenkonflikte" einfügen (mit 2 Leerzeilen davor,
    # zur Abgrenzung vom Inhaltsverzeichnis)
    if ik_idx is not None:
        ik_element = template_paragraphs[ik_idx]._element
        blank1_p = template_doc.add_paragraph("")
        ik_element.addprevious(blank1_p._element)
        blank2_p = template_doc.add_paragraph("")
        ik_element.addprevious(blank2_p._element)
        date_p = template_doc.add_paragraph(date.today().strftime("%d.%m.%Y"))
        ik_element.addprevious(date_p._element)
        if search_terms:
            terms_style = "StandardTitle2" if "StandardTitle2" in template_style_names else None
            terms_heading = template_doc.add_paragraph("Suchbegriffe", style=terms_style)
            if terms_style is None and terms_heading.runs:
                terms_heading.runs[0].bold = True
            ik_element.addprevious(terms_heading._element)
            for term in search_terms:
                term_p = template_doc.add_paragraph(f"- {term}")
                ik_element.addprevious(term_p._element)
        spacer_p = template_doc.add_paragraph("")
        ik_element.addprevious(spacer_p._element)

    # 5) Publikationsinhalt aus dem Quelldokument anhängen. Vor der ersten "Heading 2"
    # steht nur unsere eigene Standard-Titelseite (Titel, Datum, Suchbegriffe,
    # Interessenskonflikte) – die wird übersprungen, da die Vorlage das bereits hat.
    source_paragraphs = list(source_doc.paragraphs)
    theme_marker_re = re.compile(r"^(?:Themensuche|Thema):\s*„(.+)“$")
    section_marker_re = re.compile(r"^Abschnitt:\s*„(.+)“$")
    subsection_marker_re = re.compile(r"^Unterabschnitt:\s*„(.+)“$")
    section_depths = [
        m.group(1).split("\t")[0].count(".") for p in source_paragraphs
        if (m := section_marker_re.match(p.text.strip()))
    ]
    base_depth = min(section_depths) if section_depths else None
    first_heading2_idx = next(
        (i for i, p in enumerate(source_paragraphs) if p.style.name == "Heading 2"), 0
    )
    first_marker_idx = next(
        (i for i, p in enumerate(source_paragraphs)
         if theme_marker_re.match(p.text.strip()) or section_marker_re.match(p.text.strip())), None
    )
    start_idx = first_marker_idx if first_marker_idx is not None else first_heading2_idx

    author_style = "Author" if "Author" in template_style_names else None
    title_style = "StandardTitle2" if "StandardTitle2" in template_style_names else None
    biblio_title_style = "BiblioTitle1" if "BiblioTitle1" in template_style_names else None
    biblio_style = "Biblio" if "Biblio" in template_style_names else None
    image_caption_style = "ImageSubtitle" if "ImageSubtitle" in template_style_names else None
    toc1_style = "Toc1" if "Toc1" in template_style_names else None
    toc2_style = "Toc2" if "Toc2" in template_style_names else None
    toc3_style = "Toc3" if "Toc3" in template_style_names else None

    i = start_idx
    theme_counter = 0
    in_literature_section = False
    while i < len(source_paragraphs):
        para = source_paragraphs[i]
        text_stripped = para.text.strip()

        drawing_el = para._element.find(".//" + qn("w:drawing"))
        if drawing_el is not None:
            blip = drawing_el.find(".//" + qn("a:blip"))
            if blip is not None:
                embed_id = blip.get(qn("r:embed"))
                if embed_id:
                    try:
                        image_part = source_doc.part.related_parts[embed_id]
                        image_bytes = image_part.blob
                        new_img_p = template_doc.add_paragraph()
                        add_floating_picture(new_img_p, io.BytesIO(image_bytes), Inches(5.5))
                    except KeyError:
                        pass
            i += 1
            continue

        if not text_stripped:
            i += 1
            continue

        section_match = section_marker_re.match(text_stripped)
        if section_match:
            # Hauptabschnitt ("8.1<TAB>Titel") als Toc1
            sp = template_doc.add_paragraph(section_match.group(1), style=toc1_style)
            if toc1_style is None:
                for run in sp.runs:
                    run.bold = True
            i += 1
            continue

        subsection_match = subsection_marker_re.match(text_stripped)
        if subsection_match:
            # Unterabschnitt ("8.1.2<TAB>Titel") als Toc2
            up = template_doc.add_paragraph(subsection_match.group(1), style=toc2_style)
            if toc2_style is None:
                for run in up.runs:
                    run.bold = True
            i += 1
            continue

        theme_match = theme_marker_re.match(text_stripped)
        if theme_match and re.match(r"^\d+(?:\.\d+)*\t", theme_match.group(1)):
            # Thema mit fertiger Nummer aus der Gliederung ("8.1.2<TAB>Titel") als Toc2, eine Ebene
            # tiefer (unter einem Unterabschnitt, "8.1.2.1") als Toc3
            depth = theme_match.group(1).split("\t")[0].count(".")
            deep = base_depth is not None and depth >= base_depth + 2
            style = (toc3_style or toc2_style) if deep else toc2_style
            tp = template_doc.add_paragraph(theme_match.group(1), style=style)
            if style is None:
                for run in tp.runs:
                    run.bold = True
            if deep and toc3_style is None:
                tp.paragraph_format.left_indent = Pt(18)
            i += 1
            continue
        if theme_match:
            # Themensuche-Markierung: als Toc1 ("8.N") + Toc2 ("8.N.1  Themensuche: ...")
            # gegliedert, statt einzelne Publikationen zu nummerieren.
            theme_name = theme_match.group(1)
            theme_counter += 1
            if chapter_number and toc1_style:
                template_doc.add_paragraph(f"{chapter_number}.{theme_counter}", style=toc1_style)
            toc2_text = (
                f"{chapter_number}.{theme_counter}.1\t{theme_name}"
                if chapter_number else theme_name
            )
            toc2_p = template_doc.add_paragraph(toc2_text, style=toc2_style)
            if toc2_style is None:
                for run in toc2_p.runs:
                    run.bold = True
            i += 1
            continue

        if re.fullmatch(r"state of the art", text_stripped, re.IGNORECASE):
            # Überschrift des State of the Art: unterstrichen, nicht fett, mit Leerzeile darunter
            hp = template_doc.add_paragraph()
            run = hp.add_run(text_stripped)
            run.underline = True
            run.bold = False
            template_doc.add_paragraph("")
            i += 1
            continue

        if para.style.name == "Heading 2":
            # Publikationstitel: fett, Formatvorlage "StandardTitle2", OHNE eigene
            # Nummerierung – die Nummerierung liegt jetzt auf Ebene der Themensuche.
            new_p = template_doc.add_paragraph(style=title_style)
            new_p.paragraph_format.space_after = Pt(0)
            # Die Formatvorlage hat einen hängenden Einzug für das Muster "Nummer<TAB>Titel"
            # (Folgezeile rückt ein, damit sie nach dem Tab-Stopp der Nummer ausgerichtet
            # ist). Da der Titel hier KEINE Nummer+Tab mehr voranstehen hat, würde die
            # Folgezeile sonst weit eingerückt erscheinen – daher hier auf 0 zurücksetzen,
            # damit auch umgebrochene Zeilen am linken Rand stehen.
            new_p.paragraph_format.left_indent = Pt(0)
            new_p.paragraph_format.first_line_indent = Pt(0)
            run = new_p.add_run(text_stripped)
            run.bold = True

            if i + 1 < len(source_paragraphs):
                next_text = source_paragraphs[i + 1].text.strip()
                # Literaturstelle direkt nach Nennung der Publikation (an die Zitierzeile
                # angehängt) – nur die Hauptnummer der Publikation selbst, nicht die
                # vollständige Liste aller im Text zitierten externen Quellen.
                m = re.search(r"\s*\((\d+(?:,\s*\d+)*)\)\s*$", next_text)
                if m:
                    main_number = m.group(1).split(",")[0].strip()
                    next_text = next_text[: m.start()] + f" ({main_number})"
                citation_p = template_doc.add_paragraph(next_text, style=author_style)
                citation_p.paragraph_format.space_before = Pt(0)
                i += 2
                continue
            i += 1
            continue

        if para.style.name == "Heading 1" and text_stripped.lower() in ("literatur", "references"):
            in_literature_section = True
            new_p = template_doc.add_paragraph(text_stripped, style=biblio_title_style)
            if biblio_title_style is None:
                for run in new_p.runs:
                    run.bold = True
            i += 1
            continue

        if re.match(r"^(Abb\.|Tab\.)\s*\d+:", text_stripped):
            new_p = template_doc.add_paragraph(text_stripped, style=image_caption_style)
            i += 1
            continue

        if in_literature_section and re.match(r"^\d+\.\s+\S", text_stripped):
            # Eintrag der abschließenden Literaturliste (nur innerhalb des
            # "Literatur"-Abschnitts – nummerierte Zwischenüberschriften wie "1. Stärken
            # der Studie" innerhalb einer noch nicht gekürzten Würdigung dürfen NICHT
            # als Literatureintrag missverstanden werden).
            new_p = template_doc.add_paragraph(text_stripped, style=biblio_style)
            # Auch "Biblio" hat einen hängenden Einzug für "Nummer.<TAB>Text" – unsere
            # Einträge haben nach der Nummer nur ein Leerzeichen statt Tab, daher würde
            # eine umgebrochene Zeile sonst weit eingerückt erscheinen.
            new_p.paragraph_format.left_indent = Pt(0)
            new_p.paragraph_format.first_line_indent = Pt(0)
            i += 1
            continue

        # Normaler Fließtext-Absatz: Formatierung (fett/kursiv/unterstrichen) übernehmen.
        new_p = template_doc.add_paragraph()
        for run in para.runs:
            new_run = new_p.add_run(run.text)
            new_run.bold = run.bold
            new_run.italic = run.italic
            new_run.underline = run.underline
        if not para.runs:
            new_p.add_run(text_stripped)
        i += 1

    buffer = io.BytesIO()
    template_doc.save(buffer)
    return buffer.getvalue()


def build_pruefprotokoll_docx(befunde: list[dict]) -> bytes:
    """Erstellt das Prüfprotokoll der formalen Prüfung als eigenständige .docx-Datei."""
    doc = Document()
    style_document(doc)

    title_p = doc.add_paragraph()
    title_run = title_p.add_run("Prüfprotokoll – Formale Prüfung")
    title_run.bold = True
    title_run.font.name = "Arial"
    title_run.font.size = Pt(14)

    date_p = doc.add_paragraph()
    date_run = date_p.add_run(date.today().strftime("%d.%m.%Y"))
    date_run.font.name = "Arial"
    date_run.font.size = Pt(11)
    doc.add_paragraph("")

    for b in befunde:
        p = doc.add_paragraph()
        p.add_run(f"{b['status']} {b['punkt']}").bold = True
        doc.add_paragraph(b["begruendung"])
        doc.add_paragraph("")

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
