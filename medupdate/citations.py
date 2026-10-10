"""Literaturverweise im Fließtext und lokale/globale Literaturlisten.

Alle Funktionen sind unverändert aus MyLuo übernommen (rein mechanisch, deterministisch):
Klammerverweise wie "(3)" oder "(2,4)" werden in der Reihenfolge ihres ersten Auftretens
nummeriert, nicht zitierte Einträge entfallen, Verweise ohne Eintrag werden entfernt."""

from __future__ import annotations

import difflib
import re

from docx import Document


def strip_appraisal_preamble(text: str) -> str:
    """Entfernt jeglichen Vorspann der KI vor dem ersten echten Abschnitt (z.B. eine
    redundante Titelzeile 'Kritische Bewertung: ...' oder eine Metadaten-Kurzübersicht
    mit Journal/Design/Fallzahl) – unabhängig vom genauen Format dieses Vorspanns. Der
    eigentliche Inhalt beginnt meist mit dem ersten nummerierten Abschnitt (Stärken der
    Studie / Strengths of the Study); falls dieser Anker fehlt (z.B. bei reinem
    Fließtext ohne Zwischenüberschriften), werden ersatzweise führende, mit '#'
    beginnende Zeilen (redundante Markdown-Titelzeilen) entfernt."""
    lines = text.split("\n")
    start_markers = ("stärken der studie", "strengths of the study")
    for i, line in enumerate(lines):
        cleaned = re.sub(r"^\d+(\.\d+)*\.?\s*", "", line.strip().lstrip("#").strip("* "))
        if cleaned.lower().startswith(start_markers):
            return "\n".join(lines[i:])

    # Kein Abschnittsanker gefunden: trotzdem führende leere Zeilen und mit '#'
    # beginnende Markdown-Titelzeilen am Anfang entfernen.
    idx = 0
    while idx < len(lines) and (not lines[idx].strip() or lines[idx].strip().startswith("#")):
        idx += 1
    return "\n".join(lines[idx:]) if idx > 0 else text


_CITE_RE = re.compile(r"(?<![A-Za-z0-9])[(\[](\d+(?:\s*,\s*\d+)*)[)\]]")
_LIT_HEADING_RE = re.compile(r"^(literatur|references)\s*$", re.IGNORECASE)
_APPRAISAL_LABEL_RE = re.compile(r"^(kritische würdigung|critical appraisal)\s*$", re.IGNORECASE)
_ABSTRACT_LABEL_RE = re.compile(
    r"^(studie|ergebnisse|schlussfolgerung der autoren|study|results|authors'? conclusion)\s*:", re.IGNORECASE
)


def _split_cite_numbers(group: str) -> list[int]:
    return [int(x) for x in re.split(r"\s*,\s*", group.strip()) if x.strip()]


def reconcile_local_citations(text: str, entries: list[str], protected_count: int = 1) -> tuple[str, list[str]]:
    """Gleicht die lokale Literaturliste einer Publikation mit dem TATSÄCHLICH
    vorliegenden Text ab (z.B. nach einer Kürzung). Die ersten protected_count Einträge
    (Standard: 1 - die Publikation selbst) bleiben IMMER erhalten, unabhängig davon, ob sie
    im Fließtext per Klammerverweis zitiert werden (z.B. bei mehreren Hauptpublikationen
    einer gemeinsamen Würdigung, die im Text als "Publikation 1" etc. statt als "(1)"
    angesprochen werden). entries[protected_count:] sind die externen Quellen, die nur bei
    tatsächlicher Zitierung erhalten bleiben.
    - Externe Einträge bleiben nur, wenn sie im Text wirklich zitiert werden.
    - Sie werden in der Reihenfolge ihres ersten Auftretens ab Nr. protected_count+1 neu nummeriert.
    - Verweise auf nicht (mehr) vorhandene Einträge werden entfernt, statt ins Leere zu zeigen."""
    mapping = {n: n for n in range(1, protected_count + 1)}
    new_entries = list(entries[:protected_count])

    def _sub(m: re.Match) -> str:
        out: list[int] = []
        for n in _split_cite_numbers(m.group(1)):
            if n <= protected_count:
                new = n
            elif protected_count < n <= len(entries):
                if n not in mapping:
                    new_entries.append(entries[n - 1])
                    mapping[n] = len(new_entries)
                new = mapping[n]
            else:
                continue
            if new not in out:
                out.append(new)
        return f"({','.join(str(x) for x in out)})" if out else ""

    new_text = _CITE_RE.sub(_sub, text)
    new_text = re.sub(r"[ \t]+([.,;:])", r"\1", new_text)
    new_text = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", new_text)
    return new_text, new_entries


_ABBREVIATIONS = (
    "vs.", "z. B.", "z.B.", "d. h.", "d.h.", "ca.", "bzw.", "et al.", "u. a.", "u.a.", "Nr.", "Abb.", "Tab.",
    "i. d. R.", "vgl.", "Dr.", "n. s.", "sog.", "inkl.", "ggf.", "e.g.", "i.e.", "approx.",
)


def count_sentences(text: str) -> int:
    """Zählt Sätze robust gegenüber Abkürzungen ("vs.", "z. B.", "et al.") und Dezimalzahlen."""
    t = re.sub(r"\s+", " ", text.strip())
    for abbr in _ABBREVIATIONS:
        t = t.replace(abbr, abbr.replace(".", "\u2024"))  # Einpunkt-Leiter als Platzhalter
    parts = [s for s in re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ„\"(])", t) if s.strip()]
    return len(parts)


def split_sentences(text: str) -> list[str]:
    """Teilt Text in Sätze – mit derselben Abkürzungs- und Dezimalzahl-Robustheit wie
    count_sentences, gibt aber die Sätze selbst (mit wiederhergestellten Punkten) zurück."""
    t = re.sub(r"\s+", " ", text.strip())
    for abbr in _ABBREVIATIONS:
        t = t.replace(abbr, abbr.replace(".", "\u2024"))
    parts = [s for s in re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ„\"(])", t) if s.strip()]
    return [p.replace("\u2024", ".").strip() for p in parts]


_SOTA_LIT_HEAD_RE = re.compile(
    r"^[ \t]*#{0,3}[ \t]*\**[ \t]*(literatur|references)[ \t]*:?[ \t]*\**[ \t]*$", re.IGNORECASE | re.MULTILINE
)
_SOTA_LABEL_RE = re.compile(r"^state of the art\s*$", re.IGNORECASE)


def split_sota_response(raw: str) -> tuple[str, dict[int, str]]:
    """Zerlegt die Modellantwort für den State of the Art in (a) den Fließtext – als EIN Absatz,
    ohne Markdown und ohne mitgelieferte Überschriftenzeile – und (b) die Literaturliste als
    {Nummer: Eintrag}. Klammerverweise, die hinter dem Satzende stehen ("… etabliert. (1)"),
    werden vor den Satzpunkt gezogen, damit sie die Satzzählung nicht verfälschen."""
    t = re.sub(r"\*\*|^#+\s*", "", raw, flags=re.MULTILINE)
    m = _SOTA_LIT_HEAD_RE.search(t)
    body_raw, lit_raw = (t[: m.start()], t[m.end():]) if m else (t, "")

    lines = [l for l in body_raw.strip().split("\n") if l.strip()]
    if lines and re.sub(r"[^a-zA-Z ]", "", lines[0]).strip().lower() == "state of the art":
        lines = lines[1:]
    body = " ".join(" ".join(lines).split())
    body = re.sub(r"\s+([.,;:])", r"\1", body)
    body = re.sub(r"\s*([.!?])\s*(\(\d+(?:\s*,\s*\d+)*\))(?=\s|$)", r" \2\1", body)

    entries: dict[int, str] = {}
    last: int | None = None
    for line in lit_raw.split("\n"):
        em = re.match(r"^\s*\(?(\d+)\)?[.):]?\s+(.*)$", line)
        if em:
            last = int(em.group(1))
            entries[last] = em.group(2).strip()
        elif line.strip() and last is not None:
            entries[last] += " " + line.strip()
    return body, entries


def finalize_sota_citations(body: str, entries: dict[int, str]) -> tuple[str, list[str]]:
    """Nummeriert die Klammerverweise des State of the Art in der Reihenfolge ihres ersten
    Auftretens ab 1 neu. Nicht zitierte Listeneinträge entfallen, Verweise ohne Listeneintrag
    werden entfernt statt ins Leere zu zeigen. Klammern, die nur Jahreszahlen enthalten
    (z. B. "(2022)"), sind keine Literaturverweise und bleiben unangetastet."""
    old_to_new: dict[int, int] = {}
    new_entries: list[str] = []

    def _sub(m: re.Match) -> str:
        nums = _split_cite_numbers(m.group(1))
        if all(1900 <= n <= 2100 for n in nums):
            return m.group(0)
        out: list[int] = []
        for n in nums:
            if not entries.get(n, "").strip():
                continue
            if n not in old_to_new:
                new_entries.append(entries[n].strip())
                old_to_new[n] = len(new_entries)
            if old_to_new[n] not in out:
                out.append(old_to_new[n])
        return f"({','.join(str(x) for x in out)})" if out else ""

    new_body = _CITE_RE.sub(_sub, body)
    new_body = re.sub(r"[ \t]+([.,;:])", r"\1", new_body)
    new_body = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", new_body)
    return new_body.strip(), new_entries


def consolidate_literature_globally(doc: Document) -> list[str]:
    """Führt die publikationslokalen Literaturlisten des Manuskripts zu EINER Liste am
    Dokumentende zusammen – fortlaufend nummeriert in der Reihenfolge, in der die Quellen
    beim Lesen des Manuskripts erstmals auftreten: Titelzeile der Publikation (= ihre
    Hauptquelle), danach die in ihrem Text zitierten externen Quellen, dann die nächste
    Publikation usw. Nicht zitierte Listeneinträge entfallen; Verweise ohne Listeneintrag
    werden entfernt. Aufzählungen im Abstract sowie statistische Angaben wie "ICC(2,3)"
    werden nicht als Literaturverweise behandelt. Verändert das Document in place."""
    paragraphs = list(doc.paragraphs)

    # Bereits zusammengeführt (globale Überschrift "Literatur") -> nichts erneut anfassen
    for p in paragraphs:
        if p.style.name == "Heading 1" and _LIT_HEADING_RE.match(p.text.strip()):
            return []

    heading2_positions = [i for i, p in enumerate(paragraphs) if p.style.name == "Heading 2"]
    if not heading2_positions:
        return []

    # State-of-the-Art-Abschnitte (Schritt 8) stehen vor der ersten Publikation ihrer Themensuche
    # und besitzen eine EIGENE lokale Literaturliste, aber keine Hauptquelle. Sie werden als eigene
    # Blöcke in Lesereihenfolge mitnummeriert, damit ihre Verweise nicht fälschlich dem Block der
    # vorherigen Publikation zugeschlagen werden.
    sota_positions = {
        i for i, p in enumerate(paragraphs)
        if not p.style.name.startswith("Heading") and _SOTA_LABEL_RE.match(p.text.strip())
    }
    block_positions = sorted(set(heading2_positions) | sota_positions)

    counter = 1
    global_entries: dict[int, str] = {}
    elements_to_remove = []

    for idx, pos in enumerate(block_positions):
        end = block_positions[idx + 1] if idx + 1 < len(block_positions) else len(paragraphs)

        # Lokale Literaturliste dieser Publikation einlesen (Nr. 1 = Publikation selbst)
        lit_start = next(
            (i for i in range(pos + 1, end) if _LIT_HEADING_RE.match(paragraphs[i].text.strip())), None
        )
        local_map: dict[int, str] = {}
        lit_end = lit_start
        if lit_start is not None:
            j = lit_start + 1
            while j < end:
                m = re.match(r"^(\d+)\.\s+(.*)$", paragraphs[j].text.strip())
                if not m:
                    break
                local_map[int(m.group(1))] = m.group(2)
                j += 1
            lit_end = j

        if pos in sota_positions:
            # State of the Art: keine Hauptquelle. Alle Absätze außer Überschrift und lokaler Liste
            # werden auf Verweise durchsucht; Nummern in Reihenfolge des ersten Auftretens vergeben.
            sota_skip = {pos}
            if lit_start is not None:
                sota_skip.update(range(lit_start, lit_end))
            sota_scan = [i for i in range(pos + 1, end) if i not in sota_skip]
            sota_mapping: dict[int, int] = {}
            for i in sota_scan:
                for m in _CITE_RE.finditer(paragraphs[i].text):
                    nums = _split_cite_numbers(m.group(1))
                    if all(1900 <= n <= 2100 for n in nums):  # Jahreszahl in Klammern, kein Verweis
                        continue
                    for n in nums:
                        if n in sota_mapping or n not in local_map:
                            continue
                        sota_mapping[n] = counter
                        global_entries[counter] = local_map[n]
                        counter += 1

            def _sub_sota(m: re.Match, sota_mapping=sota_mapping) -> str:
                nums = _split_cite_numbers(m.group(1))
                if all(1900 <= n <= 2100 for n in nums):
                    return m.group(0)
                out: list[int] = []
                for n in nums:
                    g = sota_mapping.get(n)
                    if g is not None and g not in out:
                        out.append(g)
                return f"({','.join(str(x) for x in out)})" if out else ""

            for i in sota_scan:
                for run in paragraphs[i].runs:
                    if run.text and _CITE_RE.search(run.text):
                        run.text = re.sub(r"[ \t]+([.,;:])", r"\1", _CITE_RE.sub(_sub_sota, run.text))

            if lit_start is not None:
                for i in range(lit_start, lit_end):
                    elements_to_remove.append(paragraphs[i]._element)
                if lit_start - 1 > pos and not paragraphs[lit_start - 1].text.strip():
                    elements_to_remove.append(paragraphs[lit_start - 1]._element)
            continue

        cit_idx = pos + 1 if pos + 1 < end else None
        cit_text = paragraphs[cit_idx].text.strip() if cit_idx is not None else ""
        if 1 not in local_map:
            local_map[1] = re.sub(r"\s*[(\[]\d+(?:\s*,\s*\d+)*[)\]]\s*$", "", cit_text)

        # Welche Absätze werden auf Verweise durchsucht? Nicht: Überschrift, Zitierzeile,
        # lokale Liste, Abstract (dort stehen Aufzählungen wie "(1) Suche, (2) Screening").
        skip = {pos}
        if cit_idx is not None:
            skip.add(cit_idx)
        if lit_start is not None:
            skip.update(range(lit_start, lit_end))
        label_idx = next(
            (i for i in range(pos + 2, end) if _APPRAISAL_LABEL_RE.match(paragraphs[i].text.strip())), None
        )
        if label_idx is not None:
            skip.update(range(pos + 2, label_idx + 1))
        for i in range(pos + 2, end):
            if _ABSTRACT_LABEL_RE.match(paragraphs[i].text.strip()):
                skip.add(i)
        scan = [i for i in range(pos + 1, end) if i not in skip]

        # Hauptquelle der Publikation erhält die nächste freie Nummer
        g_main = counter
        counter += 1
        global_entries[g_main] = local_map[1]
        mapping: dict[int, int] = {1: g_main}

        # 1. Durchgang: Reihenfolge des ersten Auftretens im Text bestimmen
        for i in scan:
            for m in _CITE_RE.finditer(paragraphs[i].text):
                for n in _split_cite_numbers(m.group(1)):
                    if n in mapping or n not in local_map:
                        continue
                    mapping[n] = counter
                    global_entries[counter] = local_map[n]
                    counter += 1

        # 2. Durchgang: Verweise durch die globalen Nummern ersetzen
        removed_any = [False]

        def _sub(m: re.Match, mapping=mapping, removed_any=removed_any) -> str:
            out: list[int] = []
            for n in _split_cite_numbers(m.group(1)):
                g = mapping.get(n)
                if g is not None and g not in out:
                    out.append(g)
            if not out:
                removed_any[0] = True
                return ""
            return f"({','.join(str(x) for x in out)})"

        for i in scan:
            for run in paragraphs[i].runs:
                if run.text and _CITE_RE.search(run.text):
                    new_text = _CITE_RE.sub(_sub, run.text)
                    if removed_any[0]:
                        new_text = re.sub(r"[ \t]+([.,;:])", r"\1", new_text)
                    run.text = new_text

        # Zitierzeile unter dem Titel: Verweis auf die Hauptquelle aktualisieren
        if cit_idx is not None:
            cp = paragraphs[cit_idx]
            marker_re = re.compile(r"\s*[(\[]\d+(?:\s*,\s*\d+)*[)\]]\s*$")
            replaced = False
            for run in reversed(cp.runs):
                if run.text and marker_re.search(run.text):
                    run.text = marker_re.sub(f" ({g_main})", run.text)
                    replaced = True
                    break
            if not replaced:
                cp.add_run(f" ({g_main})")

        # Lokale Liste (samt vorangehender Leerzeile) entfernen
        if lit_start is not None:
            for i in range(lit_start, lit_end):
                elements_to_remove.append(paragraphs[i]._element)
            if lit_start - 1 > pos and not paragraphs[lit_start - 1].text.strip():
                elements_to_remove.append(paragraphs[lit_start - 1]._element)

    for el in elements_to_remove:
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)

    ordered = [global_entries[k] for k in sorted(global_entries)]
    if ordered:
        doc.add_heading("Literatur", level=1)
        for n, entry in enumerate(ordered, start=1):
            doc.add_paragraph(f"{n}. {entry}")
    return ordered


def _find_literatur_start(text: str) -> int:
    match = re.search(r"^#{0,3}\s*\**\s*(literatur|references)\s*:?\s*\**\s*$", text, re.IGNORECASE | re.MULTILINE)
    if match:
        return match.start()

    # Fallback: keine explizite Überschrift gefunden (die KI lässt "Literatur"/
    # "References" gelegentlich weg und beginnt direkt mit der nummerierten Liste).
    # Suche nach einem Block aus mind. 2 aufeinanderfolgenden, korrekt bei 1
    # beginnenden nummerierten Zeilen; nimm den letzten solchen Block im Text.
    lines = text.split("\n")
    numbered_re = re.compile(r"^\s*(\d+)\.\s+\S")
    runs = []
    i = 0
    while i < len(lines):
        m = numbered_re.match(lines[i])
        if m and int(m.group(1)) == 1:
            j = i
            expected = 1
            while j < len(lines):
                m2 = numbered_re.match(lines[j])
                if m2 and int(m2.group(1)) == expected:
                    expected += 1
                    j += 1
                else:
                    break
            length = expected - 1
            if length >= 2:
                runs.append((i, length))
            i = max(j, i + 1)
        else:
            i += 1
    if runs:
        start_idx, _ = runs[-1]
        return sum(len(l) + 1 for l in lines[:start_idx])
    return -1

def renumber_and_prune_citations(text: str) -> str:
    """Rein mechanische, garantiert korrekte Nummerierung: nummeriert Klammerverweise
    und Literaturliste anhand der tatsächlichen Reihenfolge des ersten Auftretens im
    Text neu durch (1, 2, 3, ...). Listeneinträge ohne Verwendung im Text werden
    entfernt; Klammerverweise ohne passenden Listeneintrag werden aus dem Text entfernt
    (sicherer als eine nicht auflösbare Nummer stehen zu lassen). Braucht keinen
    KI-Aufruf und ist daher schnell und 100% deterministisch."""
    lit_start = _find_literatur_start(text)
    if lit_start == -1:
        return text  # kein Literatur-/References-Abschnitt vorhanden, nichts zu tun

    body = text[:lit_start]
    lit_section = text[lit_start:]
    lines = lit_section.split("\n")
    header_match = re.match(r"^#{0,3}\s*\**\s*(literatur|references)\s*:?\s*\**\s*$", lines[0], re.IGNORECASE)
    if header_match:
        header_line = lines[0]
        entry_lines = lines[1:]
    else:
        header_line = "Literatur"  # kein echter Überschrift-Text vorhanden -> Standard einsetzen
        entry_lines = lines

    entries_by_old_num = {}
    for line in entry_lines:
        m = re.match(r"^\s*\(?(\d+)\)?[.):]?\s+(.*)$", line)
        if m:
            entries_by_old_num[int(m.group(1))] = m.group(2)
        elif line.strip() and entries_by_old_num:
            last_key = max(entries_by_old_num.keys())
            entries_by_old_num[last_key] += " " + line.strip()

    old_to_new: dict[int, int] = {}
    counter = [1]

    def replace_citation(match: re.Match) -> str:
        nums = [int(x.strip()) for x in match.group(1).split(",")]
        valid = [n for n in nums if n in entries_by_old_num]
        new_nums = []
        for n in valid:
            if n not in old_to_new:
                old_to_new[n] = counter[0]
                counter[0] += 1
            new_nums.append(old_to_new[n])
        return f"({','.join(str(x) for x in new_nums)})" if new_nums else ""

    new_body = _CITE_RE.sub(replace_citation, body)
    new_body = re.sub(r"\s+([.,;:])", r"\1", new_body)  # Leerzeichen vor Satzzeichen aufräumen

    if not old_to_new:
        return new_body.rstrip() + "\n"  # keine gültigen Zitate übrig -> Abschnitt entfällt

    new_lit_lines = [header_line]
    for old_num in sorted(old_to_new, key=lambda k: old_to_new[k]):
        new_lit_lines.append(f"{old_to_new[old_num]}. {entries_by_old_num[old_num]}")

    return new_body + "\n".join(new_lit_lines)

def extract_intext_citation_numbers(text: str) -> set[int]:
    """Findet alle im Fließtext (vor dem Literatur-Abschnitt) verwendeten Klammerverweise."""
    lit_start = _find_literatur_start(text)
    body = text[:lit_start] if lit_start != -1 else text
    numbers = set()
    for match in _CITE_RE.finditer(body):
        for n in match.group(1).split(","):
            numbers.add(int(n.strip()))
    return numbers

def extract_literature_list_numbers(text: str) -> set[int]:
    """Findet alle Nummern der Einträge im Literatur-Abschnitt."""
    lit_start = _find_literatur_start(text)
    if lit_start == -1:
        return set()
    lit_section = text[lit_start:]
    numbers = set()
    for match in re.finditer(r"^\s*(\d+)\.\s", lit_section, re.MULTILINE):
        numbers.add(int(match.group(1)))
    return numbers


def extract_and_renumber_citations_global(text: str, start_number: int) -> tuple[str, list[str]]:
    """Wie renumber_and_prune_citations, aber nummeriert in eine fortlaufende, GLOBALE
    Sequenz (beginnend bei start_number statt neu bei 1) und entfernt den lokalen
    Literatur-/References-Abschnitt komplett aus dem Text – die Einträge werden
    stattdessen zurückgegeben, um sie in die gemeinsame Literaturliste am Ende des
    Gesamtmanuskripts aufzunehmen."""
    lit_start = _find_literatur_start(text)
    if lit_start == -1:
        return text, []  # keine lokale Literaturliste vorhanden

    body = text[:lit_start]
    lit_section = text[lit_start:]
    lines = lit_section.split("\n")
    header_match = re.match(r"^#{0,3}\s*\**\s*(literatur|references)\s*:?\s*\**\s*$", lines[0], re.IGNORECASE)
    entry_lines = lines[1:] if header_match else lines
    entries_by_old_num = {}
    for line in entry_lines:
        m = re.match(r"^\s*\(?(\d+)\)?[.):]?\s+(.*)$", line)
        if m:
            entries_by_old_num[int(m.group(1))] = m.group(2)
        elif line.strip() and entries_by_old_num:
            last_key = max(entries_by_old_num.keys())
            entries_by_old_num[last_key] += " " + line.strip()

    old_to_new: dict[int, int] = {}
    counter = [start_number]
    new_entries: list[str] = []

    def replace_citation(match: re.Match) -> str:
        nums = [int(x.strip()) for x in match.group(1).split(",")]
        valid = [n for n in nums if n in entries_by_old_num]
        new_nums = []
        for n in valid:
            if n not in old_to_new:
                old_to_new[n] = counter[0]
                new_entries.append(entries_by_old_num[n])
                counter[0] += 1
            new_nums.append(old_to_new[n])
        return f"({','.join(str(x) for x in new_nums)})" if new_nums else ""

    new_body = _CITE_RE.sub(replace_citation, body)
    new_body = re.sub(r"\s+([.,;:])", r"\1", new_body)
    return new_body.rstrip(), new_entries


def dedupe_local_literature(appraisal_text: str, local_literature: list[str]) -> tuple[str, list[str]]:
    """Entfernt doppelte Einträge aus einer lokalen Literaturliste – z.B. wenn sich
    die Publikation in Abschnitt 4 versehentlich selbst zitiert und dadurch zweimal
    in der Liste auftaucht (einmal als Hauptpublikation, einmal als 'externe' Quelle).
    Erkennung erfolgt anhand von Autor+Titel-Ähnlichkeit, robust gegenüber leicht
    unterschiedlichen Zitierformaten. Die Klammerverweise im Text werden entsprechend
    umgeleitet."""
    def _core(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text[:120].lower())

    old_to_final: dict[int, int] = {}
    kept_old_indices: list[int] = []
    for idx, entry in enumerate(local_literature, start=1):
        core = _core(entry)
        match_kept = None
        for kept_idx in kept_old_indices:
            if core and difflib.SequenceMatcher(None, core, _core(local_literature[kept_idx - 1])).ratio() > 0.85:
                match_kept = kept_idx
                break
        if match_kept:
            old_to_final[idx] = old_to_final[match_kept]
        else:
            kept_old_indices.append(idx)
            old_to_final[idx] = len(kept_old_indices)

    if len(kept_old_indices) == len(local_literature):
        return appraisal_text, local_literature  # keine Duplikate gefunden

    new_list = [local_literature[i - 1] for i in kept_old_indices]

    def replace_citation(match: re.Match) -> str:
        nums = [int(x.strip()) for x in match.group(1).split(",")]
        new_nums = []
        for n in nums:
            new_n = old_to_final.get(n, n)
            if new_n not in new_nums:
                new_nums.append(new_n)
        return f"({','.join(str(x) for x in new_nums)})"

    new_text = _CITE_RE.sub(replace_citation, appraisal_text)
    return new_text, new_list


_ID_RE = re.compile(r"(?:PMID:?\s*(\d{5,9}))|(?:doi:?\s*(10\.\d{4,9}/\S+))", re.IGNORECASE)


def _ref_key(entry: str) -> str:
    m = _ID_RE.search(entry)
    if m:
        return (m.group(1) or m.group(2).rstrip(".,;")).lower()
    return re.sub(r"[^a-z0-9]+", "", entry[:120].lower())


def dedupe_global_references(doc: Document) -> int:
    """Neu in medupdate 2: Nach der Zusammenführung kann dieselbe Quelle mehrfach in der globalen
    Liste stehen (z.B. eine Leitlinie, die in zwei Themen zitiert wird). Doppelte Einträge werden
    zusammengelegt, alle Verweise umgeleitet und die Liste in Reihenfolge des ersten Auftretens neu
    nummeriert. Gibt die Anzahl entfernter Duplikate zurück."""
    paragraphs = list(doc.paragraphs)
    lit_idx = next((i for i, p in enumerate(paragraphs) if p.style.name == "Heading 1"
                    and _LIT_HEADING_RE.match(p.text.strip())), None)
    if lit_idx is None:
        return 0
    entries: dict[int, str] = {}
    entry_paras = []
    for p in paragraphs[lit_idx + 1:]:
        m = re.match(r"^(\d+)\.\s+(.*)$", p.text.strip())
        if m:
            entries[int(m.group(1))] = m.group(2)
            entry_paras.append(p)
    canon: dict[int, int] = {}
    kept: list[tuple[int, str]] = []
    for n in sorted(entries):
        key = _ref_key(entries[n])
        hit = None
        for kn, kkey in kept:
            if key == kkey or (len(key) > 30 and difflib.SequenceMatcher(None, key, kkey).ratio() > 0.92):
                hit = kn
                break
        if hit is None:
            kept.append((n, key))
            canon[n] = n
        else:
            canon[n] = hit
    removed = len(entries) - len(kept)
    if not removed:
        return 0

    body = [p for p in paragraphs[:lit_idx] if not _ABSTRACT_LABEL_RE.match(p.text.strip())]
    order: dict[int, int] = {}
    for p in body:
        for m in _CITE_RE.finditer(p.text):
            nums = _split_cite_numbers(m.group(1))
            if all(1900 <= x <= 2100 for x in nums):
                continue
            for x in nums:
                c = canon.get(x)
                if c is not None and c not in order:
                    order[c] = len(order) + 1

    def _sub(m: re.Match) -> str:
        nums = _split_cite_numbers(m.group(1))
        if all(1900 <= x <= 2100 for x in nums):
            return m.group(0)
        out: list[int] = []
        for x in nums:
            c = canon.get(x)
            if c is None:
                continue
            new = order.get(c)
            if new is not None and new not in out:
                out.append(new)
        return f"({','.join(str(v) for v in out)})" if out else ""

    for p in body:
        for run in p.runs:
            if run.text and _CITE_RE.search(run.text):
                run.text = _CITE_RE.sub(_sub, run.text)

    for p in entry_paras:
        p._element.getparent().remove(p._element)
    for c, new in sorted(order.items(), key=lambda kv: kv[1]):
        doc.add_paragraph(f"{new}. {entries[c]}")
    return removed
