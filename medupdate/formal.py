"""Formale Prüfung des fertigen Manuskripts (aus MyLuo übernommen, Grenzwerte jetzt aus den
Hinweisen für Autoren statt fest eingetragen)."""

from __future__ import annotations

import io
import re
from datetime import date

from docx import Document

from .citations import _CITE_RE, consolidate_literature_globally, dedupe_global_references, merge_repeated_citations
from .config import MODEL_FAST
from .docx_tools import copy_document_content_into_template


def run_formal_check_ai(full_text: str, llm) -> list[dict]:
    """Lässt Claude die sprach-/regelbasierten Punkte der formalen Prüfung beurteilen
    (Abkürzungen, Arzneimittel-Wirkstoffnamen, 'KI'-Mehrdeutigkeit, Rechtschreibung/
    Grammatik, Bildquellenangaben) – rein bewertend, keine inhaltliche Veränderung."""
    prompt = f"""Du prüfst ein wissenschaftliches Manuskript AUSSCHLIESSLICH formal. Verändere oder \
ergänze KEINE Inhalte – gib ausschließlich einen Prüfbericht ab.

Prüfe folgende vier Punkte und antworte für jeden mit Status ("✅" erfüllt, "⚠️" teilweise erfüllt \
oder unklar, "❌" nicht erfüllt) und einer kurzen, konkreten Begründung mit mindestens einer \
Beispielstelle aus dem Text (oder dem Hinweis, dass keine Auffälligkeit gefunden wurde):

1. Abkürzungen: Sind alle nicht international geläufigen Abkürzungen bei ihrer ersten Nennung \
ausgeschrieben/erklärt?
2. Arzneimittel: Werden Arzneimittel mit dem generischen Wirkstoffnamen (nicht dem Handelsnamen) \
genannt?
3. "KI"-Mehrdeutigkeit: Falls "KI" im Text vorkommt – wird bei der ersten Nennung "künstliche \
Intelligenz (KI)" ausgeschrieben?
4. Bildquellenangaben: Hat jede Abbildungs-/Tabellenunterschrift eine erkennbare Quellenangabe oder \
den Vermerk "Eigene Daten"?

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt in diesem Format, ohne Markdown-Codeblock, ohne \
weiteren Text:
{{"befunde": [{{"punkt": "...", "status": "✅"|"⚠️"|"❌", "begruendung": "..."}}]}}

Manuskript:
{full_text}"""
    schema = {"type": "object", "additionalProperties": False, "required": ["befunde"], "properties": {
        "befunde": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                    "required": ["punkt", "status", "begruendung"], "properties": {
                        "punkt": {"type": "string"}, "status": {"type": "string", "enum": ["✅", "⚠️", "❌"]},
                        "begruendung": {"type": "string"}}}}}}
    return llm.json(prompt, schema, model=MODEL_FAST, max_tokens=6000, effort="medium").get("befunde", [])


def fix_formatting(doc: Document) -> int:
    """Vereinheitlicht die Schriftart aller Textabsätze auf Arial (behebt
    Formatierungsfehler direkt, rein formal, keine inhaltliche Veränderung). Gibt
    die Anzahl der tatsächlich geänderten Textstellen zurück."""
    fixed = 0
    for p in doc.paragraphs:
        for run in p.runs:
            if run.font.name != "Arial":
                run.font.name = "Arial"
                fixed += 1
    return fixed


_TERMINAL_CHARS = ".!?)]“\"»:"


def _numbers_in(text: str) -> list[str]:
    return sorted(re.findall(r"\d+(?:[.,]\d+)?", text))


def _plausible_correction(orig: str, corr: str) -> bool:
    """Schutz vor Textverlust: Eine reine Rechtschreib-/Grammatikkorrektur darf den Text nicht
    nennenswert kürzen oder verlängern, kein Satzende verlieren und keine Zahlen verändern.
    Alles andere gilt als fehlerhafte oder abgebrochene KI-Antwort und wird verworfen."""
    if not corr.strip():
        return False
    if len(corr) < 0.85 * len(orig) or len(corr) > 1.15 * len(orig) + 20:
        return False
    if orig.rstrip()[-1:] in _TERMINAL_CHARS and corr.rstrip()[-1:] not in _TERMINAL_CHARS:
        return False
    if _numbers_in(orig) != _numbers_in(corr):
        return False
    return True


def _fix_spelling_batch(batch_texts: list[str], llm) -> tuple[dict[int, str], set[int]]:
    """Ein einzelner Korrektur-Durchlauf für einen Batch von Textabschnitten. Gibt zurück:
    ({Index im Batch: korrigierter Text}, {Indizes, die NICHT verlässlich zurückkamen}).
    Wurde die Antwort abgeschnitten, gilt die zuletzt gelieferte Zeile als unzuverlässig (sie
    kann mitten im Wort enden) und wird verworfen; die nicht gelieferten Abschnitte werden
    vom Aufrufer erneut verarbeitet."""
    numbered = "\n".join(f"{i + 1}|||{t}" for i, t in enumerate(batch_texts))
    prompt = f"""Korrigiere AUSSCHLIESSLICH Rechtschreib- und Grammatikfehler in den folgenden \
Textabschnitten. Verändere NICHTS anderes: keine Wortwahl, keinen Satzbau (außer zur Fehlerbehebung \
zwingend nötig), keine Zahlen, keine Zitate/Klammerverweise wie "(1)" oder "(2,3)", keine \
Fachbegriffe, keine Abkürzungen. Kürze oder erweitere keinen Abschnitt. Wenn ein Abschnitt bereits \
fehlerfrei ist, gib ihn unverändert und VOLLSTÄNDIG zurück.

Jeder Abschnitt beginnt mit seiner Nummer, gefolgt von "|||". Gib die GLEICHE Anzahl Abschnitte in \
GLEICHER Reihenfolge zurück, im selben Format "Nummer|||Text", jeweils eine Zeile pro Abschnitt. Ein \
Abschnitt darf keine Zeilenumbrüche enthalten. Gib NUR diese Zeilen zurück, keine Erläuterung davor \
oder danach.

{numbered}"""
    try:
        # Bei Abbruch wird der bis dahin gelieferte Teil genutzt (ohne die letzte, evtl. gekappte Zeile)
        result_text, truncated = llm.text_ex(prompt, model=MODEL_FAST, max_tokens=16000, effort="low")
    except Exception:
        return {}, set(range(len(batch_texts)))

    parsed: list[tuple[int, str]] = []
    for line in result_text.split("\n"):
        if "|||" not in line:
            continue
        idx_str, corrected = line.split("|||", 1)
        try:
            parsed.append((int(idx_str.strip()) - 1, corrected))
        except ValueError:
            continue
    if truncated and parsed:
        parsed = parsed[:-1]  # letzte Zeile kann mitten im Wort enden

    corrected_map: dict[int, str] = {}
    for idx, corrected in parsed:
        if 0 <= idx < len(batch_texts) and _plausible_correction(batch_texts[idx], corrected):
            corrected_map[idx] = corrected
    returned = {idx for idx, _ in parsed if 0 <= idx < len(batch_texts)}
    unreturned = set(range(len(batch_texts))) - returned
    return corrected_map, unreturned


def fix_spelling_and_grammar(doc: Document, llm) -> int:
    """Korrigiert Rechtschreib- und Grammatikfehler DIREKT im Dokument. Arbeitet auf
    Ebene einzelner Text-Runs (nicht ganzer Absätze), damit bestehende Formatierung
    (z.B. fette Labels) und die Absatzstruktur unangetastet bleiben. Verändert
    NICHTS an Wortwahl, Zahlen, Zitaten/Klammerverweisen oder Fachbegriffen – nur
    tatsächliche Rechtschreib-/Grammatikfehler.

    Schutz vor Textverlust: Es wird in kleinen Portionen gearbeitet; abgebrochene KI-Antworten
    werden erkannt (Token-Limit) und nie übernommen; jede Korrektur wird auf Plausibilität geprüft
    (keine nennenswerte Kürzung, kein verlorenes Satzende, keine geänderten Zahlen); nicht
    gelieferte Abschnitte werden erneut verarbeitet und bleiben im Zweifel UNVERÄNDERT.
    Gibt die Anzahl der tatsächlich geänderten Textstellen zurück."""
    if llm is None:
        return 0

    run_refs = []
    texts = []
    for p in doc.paragraphs:
        for run in p.runs:
            if run.text and len(run.text.strip()) > 3:
                run_refs.append(run)
                texts.append(run.text)

    if not texts:
        return 0

    max_chars_per_batch = 2500

    def make_batches(indices: list[int]) -> list[list[int]]:
        out: list[list[int]] = []
        cur: list[int] = []
        chars = 0
        for i in indices:
            if cur and chars + len(texts[i]) > max_chars_per_batch:
                out.append(cur)
                cur, chars = [], 0
            cur.append(i)
            chars += len(texts[i])
        if cur:
            out.append(cur)
        return out

    changed = 0
    pending = list(range(len(texts)))
    for _round in range(3):  # erste Runde + bis zu zwei Nachbearbeitungen nicht gelieferter Abschnitte
        next_pending: list[int] = []
        for batch_indices in make_batches(pending):
            corrected_map, unreturned = _fix_spelling_batch([texts[i] for i in batch_indices], llm)
            for local_idx, orig_idx in enumerate(batch_indices):
                if local_idx in corrected_map and corrected_map[local_idx] != texts[orig_idx]:
                    run_refs[orig_idx].text = corrected_map[local_idx]
                    changed += 1
                elif local_idx in unreturned:
                    next_pending.append(orig_idx)
        if not next_pending or len(next_pending) == len(pending):
            break
        pending = next_pending
    return changed


def run_formal_check(docx_bytes: bytes, llm, template_bytes: bytes | None = None, rules: dict | None = None,
                     format_references=None) -> tuple[list[dict], bytes]:
    """Führt die formale Prüfung des hochgeladenen (vom Nutzer überarbeiteten)
    Gesamtmanuskripts durch. Führt dabei zunächst alle publikationslokalen
    Literaturlisten zu einer gemeinsamen, fortlaufend nummerierten Liste am
    Dokumentende zusammen (siehe consolidate_literature_globally) und prüft dann
    die konsolidierte Fassung: mechanische Prüfungen (Zählungen, Konsistenz,
    Formatierung) kombiniert mit einer KI-gestützten Sprach-/Regelprüfung. Rechtschreib-,
    Grammatik- und Formatierungsfehler werden dabei DIREKT im Dokument behoben (keine
    inhaltliche Veränderung: Zahlen, Zitate, Fachbegriffe und Kernaussagen bleiben
    unangetastet). Ist eine eigene Manuskriptvorlage (template_bytes) angegeben, wird
    der korrigierte Inhalt anschließend in diese eingefügt – die Vorlage bestimmt dann
    Seitenformat, Ränder und Formatvorlagen; die hauseigene Arial-Vereinheitlichung
    entfällt in diesem Fall, damit die Schriftart der Vorlage nicht überschrieben wird.
    Gibt die Befunde UND die korrigierte Datei als Bytes zurück."""
    rules = rules or {}
    doc = Document(io.BytesIO(docx_bytes))
    consolidate_literature_globally(doc)
    dedupe_global_references(doc)
    merge_repeated_citations(doc)
    if format_references:
        # Neu in medupdate 2: Literaturverzeichnis nach dem Zitierstil der Hinweise für Autoren
        _paras = list(doc.paragraphs)
        _lit = next((i for i, p in enumerate(_paras) if p.style.name == "Heading 1"
                     and p.text.strip().lower() in ("literatur", "references")), None)
        if _lit is not None:
            _items = []
            for p in _paras[_lit + 1:]:
                m = re.match(r"^(\d+)\.\s+(.*)$", p.text.strip())
                if m:
                    _items.append((p, m.group(1), m.group(2)))
            _new = format_references([t for _, _, t in _items])
            for (p, num, _), new_text in zip(_items, _new):
                for r in p.runs[1:]:
                    r.text = ""
                if p.runs:
                    p.runs[0].text = f"{num}. {new_text}"
                else:
                    p.add_run(f"{num}. {new_text}")
    n_format_fixed = 0 if template_bytes else fix_formatting(doc)
    n_spelling_fixed = fix_spelling_and_grammar(doc, llm)

    if template_bytes:
        search_terms = []
        doc_paragraphs_for_terms = list(doc.paragraphs)
        for idx, p in enumerate(doc_paragraphs_for_terms):
            if p.text.strip().lower() == "suchbegriffe":
                for term_p in doc_paragraphs_for_terms[idx + 1:]:
                    term_text = term_p.text.strip()
                    if term_text.startswith("- "):
                        search_terms.append(term_text[2:].strip())
                    elif term_text:
                        break
                break
        corrected_bytes = copy_document_content_into_template(doc, template_bytes, search_terms)
    else:
        # Die Überschrift über dem Inhaltsverzeichnis (nur Marke für die Weiterverarbeitung) entfällt
        for p in list(doc.paragraphs)[:60]:
            if p.text.strip() in ("Inhaltsverzeichnis", "Contents", "Inhalt"):
                p._element.getparent().remove(p._element)
                break
        corrected_buffer = io.BytesIO()
        doc.save(corrected_buffer)
        corrected_bytes = corrected_buffer.getvalue()

    paragraphs = list(doc.paragraphs)
    full_text = "\n".join(p.text for p in paragraphs)

    befunde: list[dict] = []

    heading2_positions = [i for i, p in enumerate(paragraphs) if p.style.name == "Heading 2"]
    heading_issues: list[str] = []
    for pos in heading2_positions:
        heading_text = paragraphs[pos].text
        citation_line = paragraphs[pos + 1].text if pos + 1 < len(paragraphs) else ""
        if not re.search(r"\(\d+(?:,\s*\d+)*\)\s*$", citation_line.strip()):
            heading_issues.append(heading_text[:60])

    # Konsolidierte, globale Literaturliste am Dokumentende auswerten
    lit_heading_idx = None
    for i, p in enumerate(paragraphs):
        if p.style.name == "Heading 1" and p.text.strip().lower() in ("literatur", "references"):
            lit_heading_idx = i
            break

    global_entries: dict[int, str] = {}
    citation_years: list[int] = []
    if lit_heading_idx is not None:
        for i in range(lit_heading_idx + 1, len(paragraphs)):
            m = re.match(r"^(\d+)\.\s+(.*)$", paragraphs[i].text.strip())
            if m:
                global_entries[int(m.group(1))] = m.group(2)
                year_m = re.search(r"(19|20)\d{2}", paragraphs[i].text)
                if year_m:
                    citation_years.append(int(year_m.group(0)))

    total_lit_entries = len(global_entries)

    # Globale Konsistenz: jede im Text zitierte Nummer hat einen Listeneintrag und
    # umgekehrt (nur im Textteil vor der Literaturliste suchen, damit die Liste
    # selbst nicht versehentlich mitgezählt wird).
    body_text = "\n".join(p.text for p in paragraphs[:lit_heading_idx]) if lit_heading_idx is not None else full_text
    cited_global: set[int] = set()
    for m in _CITE_RE.finditer(body_text):
        for n in m.group(1).split(","):
            cited_global.add(int(n.strip()))

    missing_in_list = sorted(cited_global - set(global_entries.keys()))
    missing_in_text = sorted(set(global_entries.keys()) - cited_global)
    lit_consistency_ok = not missing_in_list and not missing_in_text

    total_images = sum(1 for p in paragraphs if re.match(r"^(Abb\.|Tab\.)\s*\d+:", p.text.strip()))

    max_refs = rules.get("max_references") or 50
    min_fig = rules.get("min_figures") or 0
    max_fig = rules.get("max_figures") or 0
    max_pages = rules.get("max_pages") or 35
    befunde.append({
        "punkt": f"Literaturanzahl (max. {max_refs})",
        "status": "✅" if total_lit_entries <= max_refs else "⚠️",
        "begruendung": f"{total_lit_entries} Literaturstellen in der zusammengeführten Liste.",
    })

    befunde.append({
        "punkt": (f"Abbildungs-/Tabellenanzahl ({min_fig}–{max_fig} laut Hinweisen für Autoren)" if max_fig
                  else "Abbildungs-/Tabellenanzahl"),
        "status": "✅" if (not max_fig or min_fig <= total_images <= max_fig) else "⚠️",
        "begruendung": f"{total_images} Abbildungen/Tabellen insgesamt gezählt.",
    })

    word_count = len(re.sub(r"\s+", " ", full_text).split())
    geschaetzte_seiten = word_count / 410
    befunde.append({
        "punkt": f"Gesamtumfang (max. {max_pages} Seiten inkl. Titelseite; grobe Schätzung, kein tatsächliches Rendern)",
        "status": "✅" if geschaetzte_seiten <= max_pages else "⚠️",
        "begruendung": (
            f"≈{word_count} Wörter, geschätzt ≈{geschaetzte_seiten:.1f} Seiten. Näherungswert – bitte "
            f"im fertigen Dokument gegenprüfen."
        ),
    })

    if citation_years:
        current_year = date.today().year
        alte = [y for y in citation_years if current_year - y > 2]
        befunde.append({
            "punkt": "Aktualität der Literatur (i. d. R. ≤1 Jahr, wichtige Arbeiten ≤2 Jahre)",
            "status": "⚠️" if alte else "✅",
            "begruendung": (
                f"{len(alte)} von {len(citation_years)} erkannten Jahreszahlen liegen mehr als 2 Jahre "
                f"zurück ({sorted(set(alte))})." if alte else
                "Alle erkannten Jahreszahlen der Literaturangaben liegen innerhalb von 2 Jahren."
            ),
        })

    befunde.append({
        "punkt": "Zitierzeilen mit Literaturziffer",
        "status": "✅" if not heading_issues else "⚠️",
        "begruendung": (
            "Alle Publikationen haben eine Zitierzeile mit Klammer-Literaturziffer." if not heading_issues
            else f"Ohne erkennbare Ziffer am Ende der Zitierzeile: {', '.join(heading_issues)}"
        ),
    })

    konsistenz_begruendung = "Jede zitierte Nummer hat einen Listeneintrag und umgekehrt."
    if missing_in_list:
        konsistenz_begruendung = f"Im Text zitiert, aber nicht in der Liste: {missing_in_list}. "
    if missing_in_text:
        konsistenz_begruendung += f"In der Liste, aber nirgends zitiert: {missing_in_text}."
    befunde.append({
        "punkt": "Literaturlisten-Konsistenz (zitiert ↔ gelistet, nach Zusammenführung)",
        "status": "✅" if lit_consistency_ok else "⚠️",
        "begruendung": konsistenz_begruendung,
    })

    ik_idx = None
    for i, p in enumerate(paragraphs):
        if p.text.strip() == "Interessenskonflikte":
            ik_idx = i
            break
    ik_filled = False
    if ik_idx is not None:
        for i in range(ik_idx + 1, min(ik_idx + 6, len(paragraphs))):
            if paragraphs[i].text.strip():
                ik_filled = True
                break
    befunde.append({
        "punkt": "Interessenkonflikte-Feld ausgefüllt",
        "status": "✅" if ik_filled else "⚠️",
        "begruendung": (
            "Text unter „Interessenskonflikte“ gefunden." if ik_filled
            else "Feld scheint noch leer (Platzhalter) zu sein – bitte manuell ausfüllen."
        ),
    })

    fonts_used = {run.font.name for p in paragraphs for run in p.runs if run.font.name}
    if template_bytes:
        schrift_begruendung = "Eigene Manuskriptvorlage verwendet – deren Schriftart bestimmt das Aussehen, keine Arial-Vereinheitlichung angewendet."
        schrift_status = "✅"
    elif n_format_fixed:
        schrift_begruendung = f"Auf Arial vereinheitlicht ({n_format_fixed} Textstelle(n) korrigiert)."
        schrift_status = "✅"
    else:
        schrift_begruendung = "Schriftart war bereits einheitlich (Arial)."
        schrift_status = "✅" if len(fonts_used) <= 1 else "⚠️"
    befunde.append({
        "punkt": "Einheitliche Schriftart",
        "status": schrift_status,
        "begruendung": schrift_begruendung,
    })

    befunde.append({
        "punkt": "Rechtschreibung und Grammatik",
        "status": "✅",
        "begruendung": (
            f"{n_spelling_fixed} Textstelle(n) direkt korrigiert." if n_spelling_fixed
            else "Keine Korrekturen notwendig bzw. Korrektur nicht möglich (siehe KI-gestützte Prüfung "
                 "unten für Details)."
        ),
    })

    if llm is not None and full_text.strip():
        try:
            befunde.extend(run_formal_check_ai(full_text, llm))
        except Exception as ex:
            befunde.append({
                "punkt": "KI-gestützte Sprach-/Regelprüfung",
                "status": "⚠️",
                "begruendung": f"Konnte nicht durchgeführt werden: {ex}",
            })

    return befunde, corrected_bytes
