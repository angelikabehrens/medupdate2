"""Offline-Test des Gesamtablaufs: KI, PubMed und Downloads werden simuliert.
Aufruf: python -m pytest tests/  (oder python tests/test_pipeline_offline.py)"""

import io
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz
from docx import Document

from medupdate import docx_tools, guidelines, manuscript, pipeline, pubmed, recommend, references

APPRAISAL = """1. Strengths of the Study
Randomized design with 1,200 patients (2).
2. Weaknesses and Limitations
Open-label design.
3. Examination of the Authors' Conclusions
Supported.
4. Context within the Current Evidence Base
Consistent with the guideline (3) and a meta-analysis (2).
5. Clinical Practice Implications
Relevant for adults.
Comment
A clear trial.
Conclusion for Practice
New, valid, practice changing for a subgroup.
References
1. Unused X. Not cited. J. 2020.
2. Meta A, Analysis B. A meta-analysis. Lancet. 2025;400:1-10. PMID: 99999999
3. German Society. S3 guideline. AWMF 2025."""

SHORT = """Study: A randomized trial in 1,200 patients.
Results: HR 0.80 (95% CI 0.70-0.90) (2).
Authors' Conclusion: Effective.
Conclusion for Practice: Practice changing for a subgroup (3)."""


class FakeLLM:
    def __init__(self):
        self.calls = []

    def text(self, prompt, **kw):
        p = prompt if isinstance(prompt, str) else json.dumps(prompt, default=str)
        self.calls.append(("text", p[:80]))
        if "Leitlinien zum folgenden Thema" in p:
            return "1. S3-Leitlinie Testthema, DGXX, 2025, https://register.awmf.org/x.pdf"
        if "ONE joint, comparative critical appraisal" in p:
            return ("Overview of the Publications\nPublication 1: RCT A.\nPublication 2: RCT B.\n"
                    "Assessment of the Linkage\nComparable populations, no pooled estimate.\n"
                    "Critical Appraisal\nBoth trials are consistent with the guideline (1).\n"
                    "Joint Conclusion\nBoth techniques are effective.\nReferences\n1. Joint Guideline X. Gut. 2025;1:1.")
        if "kürzt eine bereits erstellte gemeinsame" in p:
            return ("Publication 1: RCT A. Publication 2: RCT B. No pooled estimate. Consistent with the "
                    "guideline (1).\nJoint Conclusion: Both techniques are effective.")
        if "Du bist ein erfahrener klinisch-wissenschaftlicher Experte" in p:
            return APPRAISAL
        if "kürzt eine bereits erstellte kritische" in p:
            return SHORT
        if "gekürzte kritische Würdigung einer klinischen Studie vollständig ins" in p:
            return SHORT.replace("Study:", "Studie:").replace("Results:", "Ergebnisse:").replace(
                "Authors' Conclusion:", "Schlussfolgerung der Autoren:").replace(
                "Conclusion for Practice:", "Fazit für die Praxis:").replace("0.80", "0,80").replace(
                "0.70-0.90", "0,70-0,90")
        if "Einleitung der Publikation:" in p:
            assert "Knackpunkt" in p and "keine Stichpunkte" in p
            return "State of the Art\nThe burden is high (1). Standard care has limits (2).\nReferences\n1. Intro A. 2024.\n2. Intro B. Gut. 2023;1:1."
        if "State of the" in p and "Art" in p and "Baseline" in p:
            return "State of the Art\nThe field is established (1). Gaps remain (2).\nReferences\n1. Guideline A. 2025.\n2. Review B. Lancet. 2024;1:1."
        if "Übersetze den folgenden Absatz" in p:
            return "Das Fachgebiet ist etabliert (1). Lücken bleiben (2)."
        if "Schreibstil der Autorin" in p and "Analysiere" in p:
            return "- kurze Sätze"
        if "Passe den folgenden deutschen Text" in p:
            return p.split("Text:\n", 1)[1]
        return "OK"

    def text_ex(self, prompt, **kw):
        numbered = re.findall(r"^(\d+)\|\|\|(.*)$", prompt, re.MULTILINE)
        return "\n".join(f"{n}|||{t}" for n, t in numbered), False

    def json(self, prompt, schema, **kw):
        p = prompt if isinstance(prompt, str) else json.dumps(prompt, default=str)
        props = schema["properties"]
        if "groups" in props and "pub_ids" in json.dumps(schema):
            ids = re.findall(r"- id (\d+)", p)
            if "Thema Zwei" in p and len(ids) >= 2:
                return {"groups": [{"pub_ids": ids[:2], "question_de": "Zwei Techniken im Vergleich",
                                    "question_en": "Two techniques compared"}]}
            return {"groups": []}
        if "headings" in props:
            ids = re.findall(r"^(\d+): ", p, re.M)
            return {"headings": [{"id": i, "de": f"Schlagwort {i}", "en": f"Keyword {i}"} for i in ids]}
        if "comment_de" in props:
            assert "vier Bausteinen" in p and "KEINE Literaturziffern" in p
            return {"comment_de": "Gemeinsam zeigen beide Studien ein konsistentes Bild. Für die klinische Praxis "
                                  "bedeutet dies keine Änderung! Wir erwarten weitere Daten!",
                    "comment_en": "Together both studies are consistent. For clinical practice this means no change! "
                                  "We expect further data!"}
        if "transitions" in props:
            ids = re.findall(r"- id (\S+) \|", p)
            req = re.findall(r"- id (\S+) \|[^\n]*\| PFLICHT", p)
            assert "PFLICHT" in p and ids[0] in req, "Pflicht-Überleitung vor der ersten Publikation"
            out = [{"pub_id": i, "de": f"Wir kommen zur Arbeit {i}.", "en": f"We turn to study {i}."} for i in req]
            free = [i for i in ids[1:] if i not in req]
            if free:
                out.append({"pub_id": free[0], "de": "Vor demselben Hintergrund folgt eine weitere Arbeit.",
                            "en": "Against the same background another study follows."})
            return {"transitions": out}
        if "refs" in props:
            pid = re.search(r"- id (\S+)", p).group(1)
            assert "Höchstens EIN Rückbezug je Thema" in p
            return {"refs": [{"pub_id": pid, "manuscript": "alt.docx",
                              "sentence_de": "Im Jahr 2025 hatten wir dazu eine Vorstudie vorgestellt.",
                              "sentence_en": "In 2025 we presented a pilot study on this."}]}
        if "topics" in props:
            return {"chapter_number": "8", "chapter_title": "Gastroenterologie",
                    "topics": [{"number": "8.1.1", "title": "Thema Eins", "english_search_focus": "topic one",
                                "section_number": "8.1", "section_title": "Abschnitt"},
                               {"number": "8.1.2", "title": "Thema Zwei", "english_search_focus": "topic two",
                                "section_number": "8.1", "section_title": "Abschnitt"},
                               {"number": "8.2.1", "title": "Thema Drei", "english_search_focus": "topic three",
                                "section_number": "8.2", "section_title": "Leerer Abschnitt"},
                               {"number": "8.3.1", "title": "Thema Vier", "english_search_focus": "topic four",
                                "section_number": "8.3", "section_title": "Dritter Abschnitt"}]}
        if "citation_style" in props:
            return {k: (0 if v.get("type") == "integer" else ([] if v.get("type") == "array" else ""))
                    for k, v in props.items()} | {"max_pages": 20, "citation_style": "Vancouver", "recency_months": 12}
        if "queries" in props:
            return {"pico": "P", "queries": [{"name": "praezise", "query": "a"}, {"name": "breit", "query": "b"}],
                    "exclusion": ""}
        if "items" in props:
            ids = re.findall(r"PMID: (\d+)", p)
            # gerade n: Priorität A, ungerade n: B (geringe Praxisrelevanz); in "Thema Eins" fällt Fragestellung q1
            # wegen geringer Relevanz heraus, damit "Thema Zwei" mehr Publikationen erhält
            low = "Thema Eins" in p
            return {"items": [{"pmid": i, "study_design": "RCT", "relevance": 40 if (low and n % 3 == 1) else 90,
                               "evidence": 80 - n, "risk_of_bias": "Low", "novelty": 70,
                               "practice_impact": 80 if n % 2 == 0 else 50, "question_importance": 90 - 10 * n,
                               "journal_impact_factor": 50.0,
                               "question_key": f"q{n % 3}", "is_primary_research": True, "rationale": "gut"}
                              for n, i in enumerate(ids)]}
        if "guidelines" in props:
            return {"guidelines": [{"title": "S3-Leitlinie Test", "society": "DGXX", "year": "2025", "register_number": "",
                                    "page_url": "", "pdf_url": "https://x/y.pdf", "freely_available": True,
                                    "relevance": ""}]}
        if "entries" in props:
            ents = re.findall(r"^\d+\|\|\|(.*)$", p, re.MULTILINE)
            return {"entries": ents}
        if "index" in props:
            return {"index": 0, "typ": "Abbildung", "caption_en": "Kaplan-Meier curve", "caption_de": "Kaplan-Meier-Kurve"}
        if "assignments" in props:
            return {"assignments": []}
        if "sections" in props:  # Suchbegriffe je Hauptüberschrift (Sicherheitsnetz)
            nums = re.findall(r"^(\d+(?:\.\d+)*): ", p, re.MULTILINE)
            return {"sections": [{"number": n, "title_en": f"section {n}", "query": f"S{n}[tiab]"} for n in nums]}
        if "befunde" in props:
            return {"befunde": []}
        if "kind" in props:
            return {"title": "Unknown", "authors": [], "journal": "", "year": "", "doi": "", "kind": "other"}
        raise AssertionError(f"unerwartetes Schema {list(props)}")


def _rec(pmid):
    return {"pmid": pmid, "title": f"Trial number {pmid} of something important", "abstract": "A" * 400 + " results.",
            "authors": ["Smith J", "Doe A", "Roe B", "Poe C"], "journal": "N Engl J Med", "journal_full": "",
            "year": "2026", "volume": "394", "issue": "2", "pages": "100-110", "doi": f"10.1056/x{pmid}",
            "pmc": "", "pub_types": ["Randomized Controlled Trial"], "language": "eng"}


def _pdf_with_image(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 72), text)
    pix = fitz.Pixmap(fitz.csRGB, 400, 300, os.urandom(400 * 300 * 3), False)  # Rauschen: groß genug
    page.insert_image(fitz.Rect(50, 100, 450, 400), pixmap=pix)
    return doc.tobytes()


def _template() -> bytes:
    d = Document()
    for name in ("MainTitle1", "StandardTitle2", "Author", "Biblio", "BiblioTitle1", "ImageSubtitle", "Toc1", "Toc2"):
        d.styles.add_style(name, 1)
    d.add_paragraph("8\tGastroenterologie", style="MainTitle1")
    d.add_paragraph("Interessenkonflikte")
    d.add_paragraph("StandardTitle2 Beispiel", style="StandardTitle2")
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def test_full_pipeline(monkeypatch=None):
    recommend.MAX_PER_QUESTION = 1  # feste Auswahl für die Zählungen unten; Gruppierung siehe Schritt 2
    counter = iter(range(10000000, 10001000))
    pubmed.search = lambda q, n, email="": ([str(next(counter)) for _ in range(6)], 6)
    pubmed.fetch_records = lambda ids, email="": {i: _rec(i) for i in ids}
    pubmed.find_pmid = lambda doi="", title="", email="": None
    pipeline.unpaywall = lambda doi, email: {"is_oa": False}
    pipeline.fetch_oa_pdf = lambda doi, email: None
    guidelines.download_pdf = lambda url: _pdf_with_image("Leitlinie Empfehlung " * 400)
    references.pubmed.find_pmid = lambda **kw: None

    llm = FakeLLM()
    proj = pipeline.new_project()
    proj["email"] = "test@example.org"
    log = lambda m: print("LOG", m)
    pipeline.run_intake(llm, proj, "8 Gastro\n8.1 Thema Eins\n8.2 Thema Zwei", "Hinweise", ("t.docx", _template()), log)
    pipeline.run_recommendations(llm, proj, log)
    pipeline.run_guideline_search(llm, proj, log)
    t1, t2 = proj["topics"]["T1"], proj["topics"]["T2"]
    assert len(t1["recommended"]) == 2 and len(t2["recommended"]) == 3, "nur Priorität A, eine je Fragestellung"
    assert all(t["ranking"][p]["priority"] == "A" for t in (t1, t2) for p in t["recommended"])
    assert len(set(proj["length_plan"]["tiers"].values())) > 1, "Länge richtet sich nach der Qualität"
    assert proj["topics"]["T1"]["guidelines"][0]["text"]
    pipeline.recommendation_docx(proj)

    proj["topics"]["T3"]["enabled"] = False  # ohne Publikationen: muss aus der Gliederung verschwinden
    first = proj["topics"]["T1"]["recommended"][0]
    rec = proj["records"][first]
    uploads = {"a.pdf": _pdf_with_image(f"{rec['title']} Smith J et al. " + "body text " * 500)}
    pipeline.match_uploads(llm, proj, uploads, log)
    second = proj["topics"]["T2"]["recommended"][0]
    pipeline.match_uploads(llm, proj, {"b.pdf": _pdf_with_image(
        f"{proj['records'][second]['title']} Smith J et al. " + "intro text " * 500)}, log)
    assert proj["uploads"]["a.pdf"]["pmid"] == first
    proj["style"]["alt.docx"] = "Früheres Manuskript. " * 300
    pipeline.run_manuscript(llm, proj, log, {"figures": True, "sota": True, "style": True, "trial": True,
                                             "trial_topic": "T2"})
    assert all(p in proj["topics"]["T2"]["recommended"] for p, d in proj["pubs"].items()
               if d.get("appraisal_en") and not d.get("group")), "Probelauf zum gewählten Thema"
    done = [p for p, d in proj["pubs"].items() if d.get("appraisal_en") and not d.get("group")]
    assert len(done) == 2, "Probelauf: höchstens 2 Publikationen"
    assert pipeline.partial_docx(proj)
    pipeline.match_uploads(llm, proj, {"c.pdf": _pdf_with_image("Knee arthroplasty outcomes in athletes. " * 300)}, log)
    assert "VARIA" in proj["topics"], "Upload ohne passendes Thema kommt unter Varia"
    pipeline.run_manuscript(llm, proj, log, {"figures": True, "sota": True, "style": True})
    assert not proj["errors"], proj["errors"]
    out = proj["outputs"]
    doc = Document(io.BytesIO(out["manuskript_DE"]))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Fazit für die Praxis" in text and "Studie:" in text
    assert text.count("Im Jahr 2025 hatten wir dazu eine Vorstudie vorgestellt.") == 1, "Rückbezug auf früheres Manuskript"
    assert "Gemeinsame Würdigung: Zwei Techniken im Vergleich" in text, "gemeinsame Würdigung"
    assert "Biblio" in {p.style.name for p in doc.paragraphs}
    biblio = [p.text for p in doc.paragraphs if p.style.name == "Biblio"]
    # 6 Publikationen + je Thema 2 SotA-Quellen + Meta-Analyse + Leitlinie
    print("\n".join(biblio))
    assert any("Smith J, Doe A, Roe B et al" in b for b in biblio)
    assert not any("Unused X" in b for b in biblio), "nicht zitierte Quelle darf nicht erscheinen"
    assert any("Joint Guideline X" in b for b in biblio), "externe Quelle der gemeinsamen Würdigung"
    gid = next(p for p, d in proj["pubs"].items() if d.get("group"))
    for m in proj["pubs"][gid]["group"]:
        assert any(proj["records"][m]["doi"] in b for b in biblio), "beide Publikationen im Literaturverzeichnis"
    raw = [p.text for p in Document(io.BytesIO(out["rohfassung_DE"])).paragraphs]
    assert "State of the Art" not in raw, "State of the Art gestrichen (2026-11-03)"
    assert not any(t.get("sota_en") for t in proj["topics"].values())
    assert sum(bool((t.get("transitions") or {}).get("items")) for t in proj["topics"].values()) >= 3, \
        "jedes Thema mit Publikationen erhält eine Überleitung vor der ersten Publikation"
    assert any("Wir kommen zur Arbeit" in x for x in raw), "Pflicht-Überleitung steht im Manuskript"
    toc = [(p.style.name, p.text) for p in doc.paragraphs if p.style.name in ("Toc1", "Toc2")]
    print("GLIEDERUNG", toc)
    expected = [("Toc1", "8.1\tAbschnitt"), ("Toc2", "8.1.1\tThema Eins"), ("Toc2", "8.1.2\tThema Zwei"),
                ("Toc1", "8.2\tDritter Abschnitt"), ("Toc2", "8.2.1\tThema Vier"), ("Toc1", "8.3\tVaria"),
                ("Toc2", "8.3.1\tSchlagwort 0")]  # Varia: Schlagwort-Überschrift je Arbeit (2026-11-13)
    assert toc[:7] == expected, "Inhaltsverzeichnis auf Seite 1, leerer Abschnitt entfernt und neu nummeriert"
    assert toc[7:] == expected, "gleiche Nummern als Überschriften im Text"
    assert not any("Leerer Abschnitt" in t or "Thema Drei" in t for _, t in toc)
    texts = [p.text for p in doc.paragraphs]
    assert texts.index("8.1\tAbschnitt") < texts.index("Interessenkonflikte"), "Inhaltsverzeichnis auf der ersten Seite"
    assert "Inhalt" not in texts and "Inhaltsverzeichnis" not in texts, "ohne Überschrift „Inhalt“"
    ik = texts.index("Interessenkonflikte")
    note = texts.index(docx_tools.AI_NOTICE["Deutsch"])
    assert ik < note < texts.index("8.1\tAbschnitt", ik), "KI-Hinweis unter den Interessenkonflikten auf Seite 1"
    assert texts.count(docx_tools.AI_NOTICE["Deutsch"]) == 1
    n_img = sum(1 for p in doc.paragraphs if p.text.startswith("Abb."))
    assert n_img == 0, "keine automatischen Abbildungen mehr"
    print("BEFUNDE")
    for b in out["befunde"]:
        print(b["status"], b["punkt"], "–", b["begruendung"][:100])
    old = next(p for p, d in proj["pubs"].items() if d.get("short_en"))
    proj["pubs"][old]["short_v"] = 1
    proj["pubs"][old]["short_en"] = "ALT"
    pipeline.run_manuscript(llm, proj, log, {"figures": True, "sota": True, "style": True, "reshorten": True})
    assert proj["pubs"][old]["short_en"] != "ALT" and proj["pubs"][old]["short_v"] == pipeline.SHORT_VERSION
    blob = pipeline.export_project(proj)
    again = pipeline.import_project(blob)
    assert again["outputs"]["manuskript_DE"] == out["manuskript_DE"]
    (Path(tempfile.gettempdir()) / "medupdate_test_DE.docx").write_bytes(out["manuskript_DE"])


def test_kurznachrichten_block():
    """Kurznachrichten stehen als Block ohne Titel-/Zitierzeile, die globale Nummer im Text; Schlüsselstudien
    fett im Literaturverzeichnis; Ausrufezeichen nur im Kommentar, höchstens eines je Hauptabschnitt;
    Überleitung vor der Hinleitung; gemeinsamer Kommentar vor den Kurznachrichten (2026-10-15)."""
    from medupdate import formal, manuscript
    from medupdate.appraisal import kurznachricht

    class KurzLLM:
        def text(self, prompt, **kw):
            return "Die polnische Studie PILOT (1) prüfte X bei 100 Patienten (2). Sie fand nichts (3)."

    kn = kurznachricht(KurzLLM(), "x", "Deutsch")
    assert kn == "Die polnische Studie PILOT (1) prüfte X bei 100 Patienten. Sie fand nichts.", kn
    rec = lambda n: {"authors": [f"Autor{n} A"], "journal": "J", "year": "2026"}
    lit = lambda n: [f"Autor{n} A. Titel {n}. J. 2026;1:{n}."]
    pubs = [{"title": "Kurz Eins", "rec": rec(1), "lines": ["Studie PILOT (1) prüfte X. Nichts gefunden!"],
             "literature": lit(1), "kurz": True, "figure": None},
            {"title": "Lang Eins", "rec": rec(2), "lines": ["Hinleitung zur Frage.", "Studie: RCT mit 10 Patienten.",
                                                            "Kommentar: Praxisverändernd! Wirklich!",
                                                            "Fazit für die Praxis: Gute Studie! Nichts ändert sich."],
             "literature": lit(2), "kurz": False, "figure": None},
            {"title": "Kurz Zwei", "rec": rec(3), "lines": ["Studie ohne Nummer prüfte Y. Ergebnis positiv."],
             "literature": lit(3), "kurz": True, "figure": None}]
    pubs[1]["lines"] = pipeline.prepend_transition(pubs[1]["lines"], "Von A kommen wir zu B.")
    assert pubs[1]["lines"][0] == "Von A kommen wir zu B. Hinleitung zur Frage."
    blocks = [{"topic": "T", "number": "8.1.1", "section": ("8.1", "Abschnitt"), "sota": None, "pubs": pubs,
               "comment": "Gemeinsam gesehen (2) ändert sich nichts! Wir erwarten mehr!"}]
    raw = manuscript.build_docx("Titel", blocks, "Deutsch")
    texts = [p.text for p in Document(io.BytesIO(raw)).paragraphs]
    assert texts.index("Lang Eins") < texts.index("Kurznachrichten") < texts.index("Kurznachricht · Kurz Eins")
    assert "Kommentar: Praxisverändernd! Wirklich." in texts, "ein Ausrufezeichen je Hauptabschnitt, nur im Kommentar"
    assert "Fazit für die Praxis: Gute Studie. Nichts ändert sich." in texts
    assert "Kommentar: Gemeinsam gesehen ändert sich nichts. Wir erwarten mehr." in texts, "Themen-Kommentar ohne Ziffern"
    assert texts.index("Kommentar: Gemeinsam gesehen ändert sich nichts. Wir erwarten mehr.") < texts.index("Kurznachrichten")
    _, final = formal.run_formal_check(raw, None, _template(), {}, None, bold_refs=lit(2))
    doc = Document(io.BytesIO(final))
    texts = [p.text for p in doc.paragraphs]
    assert not any(t.startswith("Kurznachricht · ") for t in texts), "Titelzeilen der Kurznachrichten entfallen"
    assert not any(t.startswith("Autor1 A et al.") or t.startswith("Autor3 A et al.") for t in texts)
    assert "Studie PILOT (2) prüfte X. Nichts gefunden." in texts, texts
    assert "Studie ohne Nummer prüfte Y (3). Ergebnis positiv." in texts, texts
    bib = [p for p in doc.paragraphs if p.style.name == "Biblio"]
    assert [b.text[:2] for b in bib] == ["1.", "2.", "3."]
    assert all(r.bold for r in bib[0].runs) and not any(r.bold for r in bib[1].runs), "Schlüsselstudie fett"
    assert "Fett gedruckt: Schlüsselstudien dieses Kapitels." in texts
    assert pipeline.own_refs_limit(1) == 2 and pipeline.own_refs_limit(7) == 7 and pipeline.own_refs_limit(30) == 12


def test_llm_retry_and_cost():
    import types

    import anthropic
    import httpx2 as httpx

    from medupdate import llm as L

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    calls = {"n": 0}

    class Stream:
        def __enter__(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise anthropic.RateLimitError("rate", response=httpx.Response(429, request=req), body=None)
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            usage = types.SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000, cache_creation_input_tokens=0,
                                          cache_read_input_tokens=0,
                                          server_tool_use=types.SimpleNamespace(web_search_requests=3))
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="ok")],
                                         stop_reason="end_turn", usage=usage, model="claude-opus-5-5")

    seen = {}

    def stream(**kw):
        seen.update(kw)
        return Stream()

    llm = L.LLM("x")
    llm.client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=types.SimpleNamespace(stream=stream)))
    old_sleep, L.time.sleep = L.time.sleep, lambda s: None
    try:
        assert llm.text("hi", web_search=4) == "ok" and calls["n"] == 2
        assert seen["messages"][-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
        llm.budget_usd = 5.0  # bereits ≈ 6 USD verbraucht → nächster Aufruf wird verweigert
        try:
            llm.text("hi")
            raise AssertionError("Kostenlimit muss greifen")
        except L.BudgetExceeded:
            pass
        llm.budget_usd = 0.0
    finally:
        L.time.sleep = old_sleep
    assert abs(llm.cost_usd - (4.0 + 2.0 + 0.03)) < 1e-6 and llm.web_searches == 3

    def billing(**kw):
        raise anthropic.BadRequestError("Your credit balance is too low", response=httpx.Response(400, request=req),
                                        body=None)
    llm.client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=types.SimpleNamespace(stream=billing)))
    try:
        llm.text("hi")
        raise AssertionError("Kontofehler erwartet")
    except L.AccountError as ex:
        assert "Guthaben" in str(ex)


def test_results_saved_before_abort():
    from medupdate.llm import BudgetExceeded
    store = {}

    def fn(i):
        if i == 3:
            raise BudgetExceeded("Limit")
        return i * 10

    try:
        for k, v in pipeline._parallel([(i, i) for i in range(6)], fn, lambda m: None, "x", workers=1).items():
            store[k] = v
        raise AssertionError("Abbruch erwartet")
    except BudgetExceeded:
        pass
    assert store == {0: 0, 1: 10, 2: 20}, store


def test_search_broadening():
    from medupdate import recommend
    calls = []

    def fake_search(q, n, email=""):
        calls.append(q)
        return ([] if "narrow" in q else [str(20000000 + len(calls) * 10 + i) for i in range(5)]), 5

    class L:
        def json(self, prompt, schema, **kw):
            if "SENSITIVERE" in prompt:
                return {"pico": "", "queries": [{"name": "breit", "query": "wide"}], "exclusion": ""}
            return {"pico": "P", "queries": [{"name": "praezise", "query": "narrow"}], "exclusion": ""}

    old_search, old_fetch = pubmed.search, pubmed.fetch_records
    pubmed.search, pubmed.fetch_records = fake_search, (lambda ids, email="": {})
    try:
        res = recommend.recommend_for_topic(L(), {"title": "Fundusvarizen"}, "", {}, "", 6)
    finally:
        pubmed.search, pubmed.fetch_records = old_search, old_fetch
    assert len(res["attempts"]) == 3, "erweiterte Suche und längerer Zeitraum"
    assert res["window"][0] < recommend.time_window(12)[0].isoformat()

    pubmed.search = lambda q, n, email="": (_ for _ in ()).throw(RuntimeError("403"))
    try:
        recommend.collect_candidates({"queries": [{"name": "a", "query": "x"}]}, *recommend.time_window(12), "")
        raise AssertionError("Fehler muss sichtbar werden")
    except RuntimeError as ex:
        assert "PubMed" in str(ex)
    finally:
        pubmed.search = old_search


def test_length_budget():
    from medupdate.appraisal import plan_tiers
    pubs = [{"id": str(i), "length_score": s} for i, s in enumerate([90, 85, 70, 60, 50])]
    tiers, overflow = plan_tiers(pubs, {}, 1)
    assert tiers["0"] == "1,5 Seiten (Lang)" and tiers["2"] == "1 Seite" and tiers["4"] == "½ Seite (Kurz)"
    tiers, overflow = plan_tiers(pubs, {"max_pages": 4}, 1)
    assert overflow and overflow[0] == "4", "die schwächste Arbeit fällt zuerst heraus"
    assert all(t == "½ Seite (Kurz)" for t in tiers.values())
    tiers, overflow = plan_tiers(pubs, {"max_pages": 6}, 1)
    assert not overflow and tiers["0"] != "½ Seite (Kurz)" and tiers["4"] == "½ Seite (Kurz)"
    # Feste Zahl je Stufe: bei mehr Spitzenstudien fallen die schwächsten weg, statt alle zu kürzen
    from collections import Counter
    from medupdate.appraisal import estimate_pages
    counts = {"long": 3, "mid": 8, "short": 20, "news": 19}
    pubs50 = [{"id": str(i), "length_score": 79 - i * 0.5} for i in range(55)]
    tiers, overflow = plan_tiers(pubs50, {"tier_counts": counts}, 6)
    c = Counter(tiers.values())
    # Längen seit 2026-11-03: lang höchstens 1 Seite, mittel ½ Seite, kurz ⅓ Seite, Kurznachricht 0,2 Seite
    assert c["1 Seite (Lang)"] == 3 and "1,5 Seiten (Lang)" not in c and c["½ Seite (Mittel)"] == 8
    assert c["⅓ Seite (Kurz)"] == 20 and c["Kurznachricht"] == 19 and len(overflow) == 5
    budget = estimate_pages(tiers, 6)
    top = [{"id": f"t{i}", "length_score": 95 - i} for i in range(8)] + pubs50
    tiers2, overflow2 = plan_tiers(top, {"tier_counts": counts}, 6)
    # seit 2026-11-17: Spitzenstudien lang höchstens bis zum Doppelten der geplanten Zahl (hier 6), Rest mittel
    assert sum(1 for t in tiers2.values() if "Lang" in t) == 6, "höchstens doppelt so viele lange wie geplant"
    assert estimate_pages(tiers2, 6) <= budget + 3 and len(tiers2) <= 50, "Gesamtzahl bleibt (Toleranz 3 Seiten)"
    tiers3, _ = plan_tiers(pubs50, {"tier_counts": counts, "max_pages": 30}, 6)
    assert estimate_pages(tiers3, 6) <= 30 and Counter(tiers3.values())["½ Seite (Mittel)"] == 8


def test_outline_levels():
    """Vierte Gliederungsebene: Hauptabschnitt > Unterabschnitt > Thema, neu nummeriert."""
    def t(title, sec, sub=""):
        return {"title": title, "section_number": sec, "section_title": f"Abschnitt {sec}",
                "subsection_number": sub, "subsection_title": f"Unter {sub}" if sub else ""}
    proj = {"toc": {"chapter_number": "8"}, "topics": {
        "T1": t("Direkt", "8.1"),
        "T2": t("Leer", "8.2", "8.2.1"),
        "T3": t("Varizen A", "8.2", "8.2.7"),
        "T4": t("Varizen B", "8.2", "8.2.7"),
        "T5": t("Einzeln", "8.2", "8.2.8"),
    }}
    g = pipeline.outline(proj, ["T1", "T3", "T4", "T5"])
    assert g["T1"] == {"section": ("8.1", "Abschnitt 8.1"), "subsection": None, "number": "8.1.1", "level": 2}
    assert g["T3"]["section"] == ("8.2", "Abschnitt 8.2") and g["T3"]["subsection"] == ("8.2.1", "Unter 8.2.7")
    assert g["T3"]["number"] == "8.2.1.1" and g["T4"]["number"] == "8.2.1.2" and g["T4"]["subsection"] is None
    assert g["T5"]["subsection"] == ("8.2.2", "Unter 8.2.8") and g["T5"]["number"] == "8.2.2.1"

    blocks = [{"topic": proj["topics"][tid]["title"], "sota": None, "pubs": [], **g[tid]}
              for tid in ("T1", "T3", "T4", "T5")]
    raw = Document(io.BytesIO(manuscript.build_docx("Titel", blocks, "Deutsch")))
    d = Document(io.BytesIO(_template()))
    d.styles.add_style("Toc3", 1)
    b = io.BytesIO()
    d.save(b)
    out = Document(io.BytesIO(docx_tools.copy_document_content_into_template(raw, b.getvalue())))
    toc = [(p.style.name, p.text) for p in out.paragraphs if p.style.name.startswith("Toc")]
    expected = [("Toc1", "8.1\tAbschnitt 8.1"), ("Toc2", "8.1.1\tDirekt"),
                ("Toc1", "8.2\tAbschnitt 8.2"), ("Toc2", "8.2.1\tUnter 8.2.7"),
                ("Toc3", "8.2.1.1\tVarizen A"), ("Toc3", "8.2.1.2\tVarizen B"),
                ("Toc2", "8.2.2\tUnter 8.2.8"), ("Toc3", "8.2.2.1\tEinzeln")]
    # Inhaltsverzeichnis auf Seite 1: höchstens drei Ebenen (Entscheidung 2026-11-13); im Text alle Ebenen
    page1 = [e for e in expected if e[1].split("\t")[0].count(".") < 3]
    assert toc[:len(page1)] == page1, toc
    assert toc[len(page1):] == expected, toc
    texts = [p.text for p in out.paragraphs]
    i = texts.index("8.1.1\tDirekt")
    assert texts[i + 1] == "" and texts[i + 2] == "8.2\tAbschnitt 8.2", "Leerzeile zwischen Hauptabschnitten"
    assert "Suchbegriffe" not in texts and not any(re.fullmatch(r"\d\d\.\d\d\.\d{4}", t) for t in texts)
    raw_texts = [p.text for p in raw.paragraphs]
    assert "Suchbegriffe" not in raw_texts and raw_texts[raw_texts.index("8.1.1\tDirekt") + 1] == ""


def test_sota_trailing_note():
    from medupdate.citations import split_sota_response
    raw = ("State of the Art\nA gilt (1). B gilt (2).\nLiteratur\n1. Pimentel-Nunes P. ESGE. Endoscopy. 2022.\n"
           "2. Ishihara R. Guidelines. Dig Endosc. 2020.\n\nNicht verifiziert:\n1. Dass A gilt.\n2. Dass B gilt. Bitte prüfen.")
    body, entries = split_sota_response(raw)
    assert entries == {1: "Pimentel-Nunes P. ESGE. Endoscopy. 2022.", 2: "Ishihara R. Guidelines. Dig Endosc. 2020."}, entries


def test_transition_slots():
    proj = {"pubs": {p: {"short_en": "x"} for p in "abcdef"}, "tiers": {}}
    proj["pubs"]["c"]["group"] = ["c1", "c2"]
    proj["tiers"] = {"a": "⅓ Seite (Kurz)", "b": "1 Seite (Lang)", "c": "½ Seite (Mittel)", "d": "⅓ Seite (Kurz)",
                     "e": "Kurznachricht", "f": "⅓ Seite (Kurz)"}
    ready = [(p, "T") for p in "aebcdf"]
    ids, req = pipeline.transition_slots(proj, ready, "T")
    assert ids == ["a", "b", "c", "d", "f"] and req == ["a", "b", "c"]
    proj2 = {"pubs": {"x": {"short_en": "x"}, "y": {"short_en": "y"}}, "tiers": {"x": "Kurznachricht", "y": "Kurznachricht"}}
    assert pipeline.transition_slots(proj2, [("x", "K"), ("y", "K")], "K") == (["x"], ["x"])


def test_selection_package(monkeypatch_pubmed=True):
    """Auswahlliste für Autoren: HTML erzeugen, Paket der Autoren übernehmen (Auswahl, Reihenfolge, Verschieben
    in ein anderes Unterkapitel, PDF bei der Literaturstelle, zusätzliche Publikation, Leitlinie)."""
    import zipfile
    import fitz
    from medupdate import selection_sheet as S

    def pdf(text):
        doc = fitz.open()
        doc.new_page().insert_text((50, 72), text, fontsize=9)
        return doc.tobytes()
    rk = lambda q: {"quality": q, "length_score": q, "score": q, "priority": "A"}
    proj = pipeline.new_project()
    proj["toc"] = {"chapter_title": "Gastro"}
    proj["rules"] = {"max_publications": 3}
    proj["records"] = {"1": {"title": "Endoscopic submucosal dissection versus EMR for Barrett neoplasia",
                             "authors": ["Meier A"], "doi": "10.1000/xyz123"},
                       "2": {"title": "Second study", "authors": ["Kim S"]},
                       "3": {"title": "Third study", "authors": ["Rex D"]},
                       "4": {"title": "Reserve study", "authors": ["Ng K"]}}
    proj["topics"] = {"T1": {"id": "T1", "title": "Barrett", "recommended": ["1", "2"], "reserve": [],
                             "ranking": {"1": rk(90), "2": rk(70)}},
                      "T2": {"id": "T2", "title": "Polypen", "recommended": ["3"], "reserve": ["4"],
                             "ranking": {"3": rk(80), "4": rk(40)}}}
    html = S.build_sheet(proj).decode("utf-8")
    data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
                      .replace("<\\/", "</"))
    assert data["target"] == 3 and [t["id"] for t in data["topics"]] == ["T1", "T2"]
    assert [(i["id"], i["checked"]) for i in data["topics"][1]["items"]] == [("3", True), ("4", False)]
    assert S.build_sheet_zip(proj)[:2] == b"PK"
    meta = {"format": S.FORMAT, "version": 1, "project_id": proj["id"], "edited_by": "Dr. X",
            "topics": [{"id": "T1", "items": [
                {"kind": "pub", "id": "4", "checked": True},
                {"kind": "pub", "id": "1", "checked": True, "file": "dateien/001_a.pdf", "file_name": "a.pdf",
                 "check": "passt"},
                {"kind": "pub", "id": "2", "checked": False}]},
                {"id": "T2", "items": [{"kind": "pub", "id": "3", "checked": True},
                                       {"kind": "extra", "checked": True, "file": "dateien/002_x.pdf",
                                        "file_name": "x.pdf"}]}],
            "guidelines": [{"file": "leitlinien/003_g.pdf", "file_name": "g.pdf", "topic_id": "T2"}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("auswahl.json", json.dumps(meta))
        z.writestr("dateien/001_a.pdf", pdf("doi 10.1000/xyz123 Endoscopic submucosal dissection Meier"))
        z.writestr("dateien/002_x.pdf", pdf("An additional study on eosinophilic esophagitis"))
        z.writestr("leitlinien/003_g.pdf", pdf("S3 Leitlinie Kolorektales Karzinom"))
    res = pipeline.apply_selection_package(proj, buf.getvalue())
    r = res["report"]
    assert (r["selected"], r["moved"], r["pdfs"], r["extra"], r["guidelines"]) == (3, 1, 1, 1, 1), r
    assert pipeline._pub_list(proj) == [("4", "T1"), ("1", "T1"), ("3", "T2")], "Reihenfolge der Autoren"
    assert proj["pubs"]["1"]["upload_checked"] == "passt" and proj["guideline_uploads"]["g.pdf"]["topic_id"] == "T2"
    assert proj["topics"]["T1"]["ranking"]["4"] == rk(40), "Bewertung wandert mit"
    assert res["forced_topics"] == {"x.pdf": "T2"}

    class L:
        def json(self, prompt, schema, **kw):
            props = schema["properties"]
            if "kind" in props:
                return {"title": "Eosinophilic esophagitis study", "authors": ["Lee P"], "journal": "Gut",
                        "year": "2026", "doi": "", "kind": "original"}
            if "same_question" in props:  # KI möchte 1 verschieben – die Festlegung der Autoren gilt
                return {"assignments": [{"pub_id": "1", "topic_id": "T2", "reason_de": "x"}], "same_question": []}
            return {"assignments": [{"key": k, "topic_id": "T1"} for k in re.findall(r"^(\S+): ", prompt, re.M)
                                    if k.startswith("upload:")]}
    orig = pipeline.pubmed.find_pmid, pipeline.rate_unranked
    pipeline.pubmed.find_pmid = lambda **kw: None
    pipeline.rate_unranked = lambda *a, **k: None
    try:
        pipeline.check_assignment(L(), proj, res["uploads"], lambda m: None, forced_topics=res["forced_topics"])
    finally:
        pipeline.pubmed.find_pmid, pipeline.rate_unranked = orig
    cur = pipeline.current_topics(proj)
    assert cur == {"4": "T1", "1": "T1", "3": "T2", "upload:x.pdf": "T2"}, cur
    assert pipeline._pub_list(proj)[-1] == ("upload:x.pdf", "T2"), "zusätzliche Publikation an ihrer Stelle"
    assert proj["check_ok"]


def test_sota_choice():
    from medupdate.appraisal import sota_topics
    topics = {t: {"section_number": "8.1"} for t in ("A", "B", "C")} | {"D": {"section_number": "8.2"}}
    counts = {"A": 3, "B": 1, "C": 2, "D": 1}
    assert sota_topics(["A", "B", "C", "D"], topics, counts, {"A": 60, "B": 80, "C": 70, "D": 50}) == ["B", "D"]
    assert sota_topics(["A", "B", "C", "D"], topics, counts, {"A": 80, "B": 80, "C": 80}) == ["A", "D"]
    assert sota_topics(["A", "B", "C", "D"], topics, counts, {"A": 70, "B": 80, "C": 80}) == ["C", "D"]


def test_manual_window():
    from datetime import date
    assert recommend.manual_window(["2025-03-01", "2026-02-28"]) == (date(2025, 3, 1), date(2026, 2, 28))
    assert recommend.manual_window(None) is None
    seen = []
    orig = recommend.collect_candidates
    recommend.collect_candidates = lambda plan, von, bis, email, limit=60, **kw: (seen.append((von, bis)) or ([], {}))
    try:
        res = recommend.recommend_for_topic(FakeLLM(), {"title": "Thema Eins"}, "Gastro", {}, "x@y.z", 6,
                                            window=["2025-03-01", "2026-02-28"])
    finally:
        recommend.collect_candidates = orig
    assert set(seen) == {(date(2025, 3, 1), date(2026, 2, 28))}, "manueller Zeitraum wird nicht verlängert"
    assert res["window"] == ["2025-03-01", "2026-02-28"]


def test_manual_limits():
    proj = {"rules": {"max_pages": 30, "max_references": 80}, "manual_limits": {"pages": 20, "references": 50}}
    pipeline.apply_manual_limits(proj)
    assert proj["rules"]["max_pages"] == 31 and proj["rules"]["max_references"] == 60, proj["rules"]
    proj["manual_limits"] = {"pages": 0, "references": 0}
    pipeline.apply_manual_limits(proj)
    assert proj["rules"]["max_pages"] == 30 and proj["rules"]["max_references"] == 80, "zurück zu den Hinweisen"
    proj["limit_tolerance"] = {"mode": "percent", "value": 10}
    proj["manual_limits"] = {"pages": 20, "references": 50}
    pipeline.apply_manual_limits(proj)
    assert proj["rules"]["max_pages"] == 23 and proj["rules"]["max_references"] == 55


def test_split_intro():
    from medupdate.appraisal import split_intro
    ft = ("Title\nAbstract\nShort.\nIntroduction\nVarices bleed often [1]. Therapy is standard [2].\n"
          "Methods\nWe did things.\n" + "x " * 500 + "\nReferences\n1. A B. Gut. 2020.\n2. C D. Lancet. 2021.")
    intro, refs = split_intro(ft)
    assert intro.startswith("Varices") and "Methods" not in intro and "We did" not in intro
    assert refs.startswith("1. A B")


def test_repeated_citations():
    from medupdate.citations import merge_repeated_citations
    d = Document()
    p = d.add_paragraph()
    p.add_run("Kommentar: ").bold = True
    p.add_run("Die Studie zeigt A (1). Im Jahr (2024) fand sich B (1,2). Der Effekt hielt an (1).")
    d.add_paragraph("Ergänzend gilt C (3). Die Daten stützen D (1).")
    d.add_paragraph("Studie: (1) Suche, (1) erneut")
    assert merge_repeated_citations(d) == 2
    assert d.paragraphs[0].text == "Kommentar: Die Studie zeigt A. Im Jahr (2024) fand sich B (2). Der Effekt hielt an (1).", \
        d.paragraphs[0].text
    assert d.paragraphs[1].text == "Ergänzend gilt C (3). Die Daten stützen D (1).", "neuer Absatz: wieder nennen"
    d2 = Document()
    d2.add_paragraph("A (1). B (1). C (4)")
    merge_repeated_citations(d2)
    assert d2.paragraphs[0].text == "A. B. C (1,4)", d2.paragraphs[0].text


def test_reassign_topics():
    class L:
        calls = 0

        def json(self, prompt, schema, **kw):
            L.calls += 1
            assert "Barrettkarzinom" in prompt and "[hochgeladen]" in prompt
            return {"assignments": [{"pub_id": "1", "topic_id": "T2", "reason_de": "Technischer Aspekt der Resektion."},
                                    {"pub_id": "2", "topic_id": "T1", "reason_de": ""},
                                    {"pub_id": "X", "topic_id": "T1", "reason_de": "Passt zu Barrett."}],
                    "same_question": [{"pub_ids": ["3", "2"], "question_de": "ESD versus EMR"}]}
    rk = {"question_key": "q", "length_score": 70}
    proj = {"toc": {"chapter_title": "GI"}, "records": {p: {"title": f"Studie {p}"} for p in ["1", "2", "3", "X"]},
            "topics": {"T1": {"id": "T1", "title": "Barrettkarzinom", "number": "1.1", "section_number": "1",
                              "section_title": "Ösophagus", "recommended": ["1", "2"], "ranking": {"1": rk, "2": rk}},
                       "T2": {"id": "T2", "title": "Endoskopische Resektion", "number": "1.2", "section_number": "1",
                              "section_title": "Ösophagus", "recommended": ["3"], "ranking": {"3": rk}}},
            "pubs": {"X": {"extra": True, "topic_id": "", "full_text": "Barrett text"}}}
    pipeline.reassign_topics(L(), proj, lambda m: None)
    cur = pipeline.current_topics(proj)
    # 1 technisch → Resektion; 2 und 3 gleiche Fragestellung → gemeinsames Thema (das von 3); X aus Varia → Barrett
    assert cur == {"1": "T2", "2": "T2", "3": "T2", "X": "T1"}, cur
    assert proj["topics"]["T2"]["ranking"]["1"] == rk
    assert proj["question_groups"] == [{"pmids": ["3", "2"], "question_de": "ESD versus EMR"}]
    ov = [r for r in pipeline.assignment_overview(proj) if r["kind"] == "topic"]
    t2 = {x["id"]: x for x in ov[1]["pubs"]}
    assert t2["1"]["moved_from"] == "Barrettkarzinom" and t2["2"]["joint"] == "ESD versus EMR"
    pipeline.reassign_topics(L(), proj, lambda m: None)
    assert L.calls == 1, "unveränderte Zuordnung wird nicht erneut geprüft"
    pipeline.set_manual_topic(proj, "1", "T1")
    assert pipeline.current_topics(proj)["1"] == "T1"
    pipeline.reassign_topics(L(), proj, lambda m: None)
    assert L.calls == 1 and pipeline.current_topics(proj)["1"] == "T1", "eigene Festlegung bleibt"
    assert pipeline.assignment_docx(proj)[:2] == b"PK"


def test_enforce_pub_limit():
    def it(q):
        return {"quality": q, "length_score": q, "evidence": q}
    proj = {"rules": {}, "manual_limits": {"publications": 3},
            "records": {p: {"title": f"Studie {p}"} for p in "abcde"},
            "topics": {"T1": {"id": "T1", "title": "Eins", "recommended": ["a", "b", "c"],
                              "ranking": {"a": it(90), "b": it(40), "c": it(70)}},
                       "T2": {"id": "T2", "title": "Zwei", "recommended": ["d"], "ranking": {"d": it(60), "e": it(10)}}},
            "pubs": {"e": {"extra": True, "topic_id": "T2"}}}
    dropped = pipeline.enforce_pub_limit(proj, lambda m: None)
    assert [e["id"] for e in dropped] == ["e", "b"], dropped
    assert set(pipeline.current_topics(proj)) == {"a", "c", "d"}
    assert "b" not in proj["topics"]["T1"].get("reserve", []) and proj["pubs"]["e"]["limit_dropped"]
    rev = proj["sel_rev"]
    proj["manual_limits"] = {"publications": 4}
    pipeline.enforce_pub_limit(proj, lambda m: None)
    assert set(pipeline.current_topics(proj)) == {"a", "b", "c", "d"} and proj["sel_rev"] > rev
    proj["manual_limits"] = {"publications": 0}
    assert pipeline.enforce_pub_limit(proj, lambda m: None) == [] and len(pipeline.current_topics(proj)) == 5
    # Seitenumfang: 4 Seiten inkl. Titelseite reichen nicht für alle fünf
    proj["manual_limits"] = {"pages": 3}
    proj["limit_tolerance"] = {"mode": "absolute", "value": 0}
    dropped = pipeline.enforce_pub_limit(proj, lambda m: None)
    assert dropped and dropped[0]["id"] == "e" and "Seitenumfang" in dropped[0]["reason"]


def test_direct_upload():
    import fitz
    def pdf(text):
        doc = fitz.open()
        doc.new_page().insert_text((50, 72), text, fontsize=9)
        return doc.tobytes()
    rec = {"title": "Endoscopic submucosal dissection versus EMR for Barrett neoplasia", "authors": ["Meier A"],
           "doi": "10.1000/xyz123"}
    proj = {"records": {"1": rec}, "pubs": {}, "uploads": {}}
    bad = pipeline.attach_direct_upload(proj, "1", "falsch.pdf", pdf("A completely different trial on liver cirrhosis outcomes"))
    assert not bad["ok"] and "liver" in bad["found"] and "1" not in proj["pubs"]
    good = pipeline.attach_direct_upload(proj, "1", "richtig.pdf", pdf("doi 10.1000/xyz123 Endoscopic submucosal dissection"))
    assert good["ok"] and proj["pubs"]["1"]["upload_name"] == "richtig.pdf" and proj["uploads"]["richtig.pdf"]["pmid"] == "1"
    forced = pipeline.attach_direct_upload(proj, "1", "falsch.pdf", pdf("Another unrelated paper about colon"), force=True)
    assert forced["ok"] and proj["pubs"]["1"]["upload_checked"] == "von Ihnen bestätigt"


def test_fazit_and_headings():
    class L:
        def json(self, prompt, schema, **kw):
            if "Überschriften" in prompt:
                ids = re.findall(r"^(\d+): (.+)$", prompt, re.M)
                return {"items": [{"id": i, "de": {"Esophagus": "Ösophagus", "Stomach": "Magen"}.get(x, x)}
                                  for i, x in ids]}
            ids = re.findall(r"^id (\d+):", prompt, re.M)
            return {"items": [{"id": i, "sentence": f"Variante {i} zeigt erstmals etwas."} for i in ids]}
    proj = {"toc": {"chapter_title": "Endoscopy"}, "pubs": {
        "a": {"short_de": "Studie: x\nFazit für die Praxis: Das Ergebnis ist neu. Es ist valide (1)."},
        "b": {"styled_de": "Kommentar: y\nFazit für die Praxis: Das Ergebnis ist neu. Praxis ändert sich nicht."},
        "c": {"short_de": "Fazit für die Praxis: Die Arbeit bestätigt frühere Daten."}},
        "topics": {"T": {"title": "Stomach", "section_title": "Esophagus"}}}
    n = pipeline.vary_fazit_openings(L(), proj, [("a", "T"), ("b", "T"), ("c", "T")], lambda m: None)
    assert n == 2
    assert proj["pubs"]["a"]["short_de"].endswith("Fazit für die Praxis: Variante 0 zeigt erstmals etwas. Es ist valide (1).")
    assert "Variante 1" in proj["pubs"]["b"]["styled_de"] and "bestätigt" in proj["pubs"]["c"]["short_de"]
    assert pipeline.vary_fazit_openings(L(), proj, [("a", "T"), ("b", "T")], lambda m: None) == 0
    de = pipeline.headings_de(L(), proj, ["T"])
    assert de["Esophagus"] == "Ösophagus" and de["Stomach"] == "Magen"


def test_comment_box():
    from docx import Document
    from medupdate.docx_tools import is_shaded, render_line_with_bold_label
    doc = Document()
    box = False
    for line in ["Studie: A.", "Kommentar: B.", "Weiter.", "Fazit für die Praxis: C."]:
        box = render_line_with_bold_label(doc, line, box)
    ps = doc.paragraphs
    assert not is_shaded(ps[0]) and ps[0].runs[0].underline, "Studie: unterstrichen, ohne Kasten"
    assert all(is_shaded(p) for p in ps[1:]), "Kommentar, Folgeabsatz und Fazit im grauen Kasten"
    assert ps[1].runs[0].bold and not ps[1].runs[0].underline, "Kommentar: fett, nicht unterstrichen"
    assert ps[3].runs[0].bold and not ps[3].runs[0].underline, "Fazit: fett, nicht unterstrichen"


def test_compact_note():
    from medupdate.appraisal import _compact_note
    assert _compact_note(None, "1 Seite") == ""
    n = _compact_note({"stats": True, "comment": True}, "½ Seite (Kurz)")
    assert "STATISTIK KOMPAKT" in n and "höchstens 1 kurze" in n and "KOMMENTAR KOMPAKT" in n
    assert "höchstens 3 kurze" in _compact_note({"comment": True}, "1,5 Seiten (Lang)")


def test_guideline_needs():
    """Nur Leitlinien, die die KI nicht hat und die die Würdigungen verbessern, werden angefragt."""
    from medupdate import selection_sheet as S
    calls = []

    class L:
        def json(self, prompt, schema, **kw):
            calls.append(prompt)
            assert "NICHT verfügbar" in prompt and "Barrett study" in prompt
            return {"requests": [{"index": 1, "reason": "Für die Therapieempfehlungen"},
                                 {"index": 0, "reason": "liegt schon vor"}, {"index": 9, "reason": "x"}]}
    proj = pipeline.new_project()
    proj["records"] = {"1": {"title": "Barrett study"}}
    proj["topics"] = {
        "T1": {"id": "T1", "title": "Barrett", "recommended": ["1"],
               "guidelines": [{"title": "S2k Barrett", "text": "x" * 4000}, {"title": "ESGE Barrett"},
                              {"title": "ACG Barrett"}]},
        "T2": {"id": "T2", "title": "Leer", "recommended": ["1"], "guidelines": [{"title": "G", "text": "y"}]}}
    pipeline.run_guideline_needs(L(), proj, lambda *a: None)
    assert proj["topics"]["T1"]["gl_need"]["requests"] == [{"index": 1, "reason": "Für die Therapieempfehlungen"}]
    assert proj["topics"]["T2"]["gl_need"]["requests"] == [] and len(calls) == 1
    pipeline.run_guideline_needs(L(), proj, lambda *a: None)  # unverändert → kein neuer Aufruf
    assert len(calls) == 1
    gl = S._sheet_guidelines(proj["topics"]["T1"])
    assert [g["need"] for g in gl] == ["", "ja", "nein"] and gl[1]["reason"]
    assert S._sheet_guidelines({"guidelines": [{"title": "A"}]})[0]["need"] == ""  # altes Projekt


def test_search_plan_and_safety_net():
    """Suchplan mit Kapitelanker (Krankheit statt pauschal „surgery“), nicht suchbare Überschrift, eigene
    Suchbegriffe, Bearbeiten nur der geänderten Themen und Sicherheitsnetz für neue Themen."""
    from medupdate import recommend as R
    calls = {"plan": 0, "rank": [], "searches": []}

    class L:
        def json(self, prompt, schema, **kw):
            props = schema["properties"]
            if "anchor_query" in props:
                assert "Kuriosa" in prompt and "Nachsorge › Compliance" in prompt
                return {"chapter_en": "Abdominal aortic disease", "structure": "krankheit", "structure_note": "x",
                        "anchor_query": 'AAA[tiab]', "journals": ["J Vasc Surg"],
                        "topics": [{"id": "T1", "title_en": "EVAR planning", "searchable": True,
                                    "aspect_query": "EVAR[tiab]", "note": ""},
                                   {"id": "T2", "title_en": "adherence to surveillance", "searchable": True,
                                    "aspect_query": "", "note": ""},
                                   {"id": "T3", "title_en": "", "searchable": False, "aspect_query": "", "note": ""}]}
            if "queries" in props:
                if "SENSITIVERE" not in prompt:  # Erweiterung bei wenigen Treffern ist kein neuer Suchplan
                    calls["plan"] += 1
                    assert "Kapitel-Anker (PubMed): AAA[tiab]" in prompt and "Gliederungspfad" in prompt
                return {"pico": "P", "queries": [{"name": "praezise", "query": "AAA[tiab] AND x"},
                                                 {"name": "breit", "query": "AAA[tiab]"}], "exclusion": ""}
            if "items" in props:
                ids = re.findall(r"PMID: (\d+)", prompt)
                calls["rank"].append(ids)
                return {"items": [{"pmid": i, "study_design": "RCT", "relevance": 90, "evidence": 85,
                                   "risk_of_bias": "Low", "novelty": 80, "practice_impact": 85,
                                   "question_importance": 80, "journal_impact_factor": 10.0,
                                   "question_key": f"k{i}", "is_primary_research": True, "rationale": "r"}
                                  for i in ids]}
            if "assignments" in props:
                return {"assignments": [{"key": "900", "topic_id": "T2"}, {"key": "901", "topic_id": ""}]}
            raise AssertionError(list(props))

    def fake_search(q, retmax, email=""):
        calls["searches"].append(q)
        assert "[Date - Entrez]" in q
        if "netz_" in q:
            pass
        ids = ["900", "901"] if q.startswith("((AAA[tiab]) AND (randomized") else ["100", "101"]
        if "eigene" in q or "sac embolization" in q:
            ids = ["102"]
        return (ids[:retmax] if retmax else []), 42

    orig = (pubmed.search, pubmed.fetch_records)
    pubmed.search = fake_search
    pubmed.fetch_records = lambda ids, email="": {i: _rec(i) for i in ids}
    try:
        proj = pipeline.new_project()
        proj["email"] = "x@y.z"
        proj["toc"] = {"chapter_title": "Abdominelle Aortenerkrankungen"}
        proj["topics"] = {
            "T1": {"id": "T1", "title": "EVAR-Planung", "section_title": "Versorgung"},
            "T2": {"id": "T2", "title": "Compliance", "section_title": "Nachsorge"},
            "T3": {"id": "T3", "title": "Kuriosa", "section_title": "Diverses"}}
        pipeline.run_search_plan(L(), proj, lambda *a: None)
        assert calls["plan"] == 2 and proj["topics"]["T3"]["searchable"] is False
        assert proj["topics"]["T1"]["plan_counts"] == {"praezise": 42, "breit": 42}
        assert proj["search_frame"]["counts"]["netz_journale"] == 42
        assert '"J Vasc Surg"[ta]' in R.safety_queries(proj["search_frame"])[1]["query"]
        proj["topics"]["T1"]["manual_query"] = "sac embolization"
        pipeline.run_recommendations(L(), proj, lambda *a: None)
        t1, t2 = proj["topics"]["T1"], proj["topics"]["T2"]
        assert "102" in t1["ranking"] and t1["manual_query"] == "sac embolization"
        assert [q["name"] for q in t1["plan"]["queries"]] == ["praezise", "breit"]  # eigene bleibt separat
        assert proj["topics"]["T3"]["recommended"] == []
        assert t2["ranking"]["900"]["via"] == "Sicherheitsnetz" and "900" in t2["recommended"]
        neu = proj["topics"][pipeline.SAFETY]
        assert neu["recommended"] == ["901"] and neu["enabled"] is False
        n_rank = len(calls["rank"])
        # Nur das geänderte Thema wird neu bewertet; das Sicherheitsnetz läuft nicht noch einmal
        t2["plan"]["queries"][0]["query"] = "AAA[tiab] AND adherence"
        t2["plan_rev"] += 1
        pipeline.run_recommendations(L(), proj, lambda *a: None)
        assert len(calls["rank"]) == n_rank + 1
        assert "900" in proj["topics"]["T2"]["ranking"]  # Zuordnung aus dem Sicherheitsnetz bleibt erhalten
        # Vorjahresmanuskript: Titel mit Silbentrennung aus dem PDF und DOI werden erkannt
        from medupdate import prior
        proj["prior"] = prior.build_prior({"Vorjahr.pdf": "1. Smith J et al. Trial num-\nber 100 of something "
                                                          "important. N Engl J Med 2025.\n2. Doe A. Other. "
                                                          "doi:10.1056/X900."})
        assert prior.match(_rec("101"), proj["prior"]) == ""
        assert pipeline.apply_prior(proj) == 2
        t1, t2 = proj["topics"]["T1"], proj["topics"]["T2"]
        assert "100" not in t1["ranking"] and "100" not in t1["recommended"] and "101" in t1["recommended"]
        assert t1["prior_excluded"]["100"]["why"] == "Titel" and t2["prior_excluded"]["900"]["why"] == "DOI"
        assert "900" not in t2["recommended"]
        n_rank = len(calls["rank"])
        pipeline.run_recommendations(L(), proj, lambda *a: None)  # bleibt ausgeschlossen, keine neue Bewertung
        assert "900" not in proj["topics"]["T2"]["ranking"] and len(calls["rank"]) == n_rank
        proj.pop("prior")
        assert pipeline.apply_prior(proj) == 0
        assert "100" in t1["recommended"] and "900" in t2["recommended"] and "prior_excluded" not in t1
    finally:
        pubmed.search, pubmed.fetch_records = orig


def test_chapter_selection():
    """Angelika 2026-11-09: relative Empfehlung bis zur Zielzahl, Reserve auf 8 aufgefüllt, Hintergrundliteratur,
    Bewertung im passenden Thema, Warnung beim falschen Vorjahresmanuskript, strengerer Titelabgleich."""
    from medupdate import prior
    def it(rel, ev, imp, nov=60, primary=True, key=""):
        return {"relevance": rel, "evidence": ev, "practice_impact": imp, "novelty": nov, "risk_of_bias": "Low",
                "question_importance": 70, "journal_impact_factor": 5, "is_primary_research": primary,
                "question_key": key, "study_design": "RCT", "rationale": "r"}
    proj = pipeline.new_project()
    proj["toc"] = {"chapter_title": "pAVK konservativ"}
    proj["manual_limits"] = {"publications": 5}
    pipeline.apply_manual_limits(proj)
    proj["topics"] = {
        "T1": {"id": "T1", "title": "PCSK9", "ranking": {
            "1": it(90, 90, 80), "2": it(80, 70, 50, key="a"), "3": it(75, 60, 40, key="b"),
            "4": it(60, 50, 30, key="c"), "5": it(45, 40, 20, key="d"), "6": it(30, 40, 20, key="e"),
            "R": it(85, 40, 30, primary=False)}},
        "T2": {"id": "T2", "title": "Lp(a)", "ranking": {
            "2": it(40, 70, 50, key="a"), "7": it(85, 75, 55, key="f"), "8": it(70, 65, 45, key="g")}},
    }
    proj["records"] = {p: {"pmid": p, "title": f"Titel {p}", "authors": ["Muster A"], "abstract": "x",
                           "journal": "J", "year": "2025", "pub_types": []} for p in "12345678R"}
    pipeline.select_chapter(proj)
    t1, t2 = proj["topics"]["T1"], proj["topics"]["T2"]
    rec = t1["recommended"] + t2["recommended"]
    assert len(rec) == 5 and "1" in rec and "6" not in rec, rec  # Zielzahl 5, Relevanz 30 nie empfohlen
    assert "2" in t1["recommended"] and "2" not in t2["recommended"] + t2["reserve"]  # bestes Thema T1
    assert t1["ranking"]["2"]["priority"] != "A"  # relative Empfehlung auch ohne Priorität A
    assert "R" not in rec and t1["background"] == ["R"]  # Review nur als Hintergrundliteratur
    assert "6" not in t1["reserve"] and set(t1["recommended"] + t1["reserve"]) >= {"1", "2", "3", "4", "5"}
    # Bewertung im passenden Thema: gute Arbeit 8 passt laut Zuordnung besser zu T1
    proj["topics"]["T2"]["ranking"]["8"]["relevance"] = 50
    calls = []

    class L:
        def json(self, prompt, schema, **kw):
            props = schema.get("properties", {})
            if "assignments" in props:
                return {"assignments": [{"key": "8", "topic_id": "T1"}]}
            calls.append(prompt)
            return {"items": [{**it(88, 65, 60, key="g"), "pmid": "8"}]}
    pipeline.run_reroute(L(), proj, lambda *a: None)
    assert t1["ranking"]["8"]["via"] == "Themenzuordnung" and len(calls) == 1
    pipeline.run_reroute(L(), proj, lambda *a: None)  # gemerkt: kein zweiter Aufruf
    assert len(calls) == 1
    # Kandidatenzahl bei breiten Themen
    assert recommend.candidate_limit({"breit": 17959, "praezise": 400}) == recommend.HUGE_CANDIDATES
    assert recommend.candidate_limit({"breit": 17959, "praezise": 300}) == recommend.BROAD_CANDIDATES
    assert recommend.candidate_limit({"praezise": 100, "breit": 900, "x": "Fehler"}) == 100  # kleines Thema vollständig
    assert recommend.candidate_limit({"praezise": 55, "breit": 900}) == 60
    assert recommend.candidate_limit({"praezise": 248, "breit": 292}) == recommend.BROAD_CANDIDATES
    # Titelabgleich nur mit Erstautor in der Nähe; Warnung beim aktuellen Jahrgang
    pr = prior.build_prior({"8 pAVK Angio Update 2026.pdf": "Samuel M, Berry C. Long-term trials of colchicine "
                            "for secondary prevention of vascular events: a meta-analysis. Eur Heart J 2025."})
    other = {"title": "Colchicine for secondary prevention of vascular events: a meta-analysis of trials.",
             "authors": ["Fiolet ATL"]}
    own = {"title": "Long-term trials of colchicine for secondary prevention of vascular events: a meta-analysis.",
           "authors": ["Samuel M"]}
    assert prior.match(other, pr) == "" and prior.match(own, pr) == "Titel"
    assert "Vorjahres" in prior.warning(pr, 1, ["2025-03-01", "2026-03-31"])
    assert prior.warning(pr, 1, ["2026-03-01", "2027-03-31"]) == ""
    assert "16 der" in prior.warning(pr, 16, ["2026-03-01", "2027-03-31"])


def test_huge_topics_evidence_first():
    """Angelika 2026-11-14 (Vergleiche Psoriasis und Atopie): mehr als 300 präzise Treffer → bis zu 300
    Kandidaten, Treffer der präzisen Suche mit hoher Evidenz zuerst; kleinere Themen unverändert; ein schon
    bewertetes Thema mit 100 Kandidaten bewertet beim Erweitern nur die neuen."""
    searched = []

    def fake_search(q, retmax, email=""):
        searched.append((q, retmax))
        if '"N Engl J Med"[ta]' in q:  # Vorrang führender Journale: hier ohne Treffer (eigener Test)
            return [], 0
        if "registry[tiab]" in q:
            return [f"E{i}" for i in range(min(retmax, 250))], 250
        tag = "P" if q.startswith("(PRAEZ") else "B"
        n = 900 if tag == "P" else 3000
        return [f"{tag}{i}" for i in range(min(retmax, n))], n

    def rec(p):
        return {"pmid": p, "title": f"T {p}", "abstract": "a" * 400, "pub_types": [], "language": "eng"}

    orig = (pubmed.search, pubmed.fetch_records)
    pubmed.search = fake_search
    pubmed.fetch_records = lambda ids, email="": {i: rec(i) for i in ids}
    try:
        plan = {"pico": "p", "queries": [{"name": "praezise", "query": "PRAEZ"}, {"name": "breit", "query": "BREIT"}]}
        prep = recommend.prepare_topic(None, {"title": "Systemtherapie"}, "Atopie", {}, "", plan=plan,
                                       window=["2025-07-01", "2026-10-31"])
        assert prep["limit"] == 300 and prep["evidence_hits"] == 250
        ids = [c["pmid"] for c in prep["new"]]
        assert len(ids) == 300 and ids[:200] == [f"E{i}" for i in range(200)] and "E200" not in ids
        assert ids[200:202] == ["P0", "B0"]  # danach wie bisher im Reißverschluss
        assert any("registry[tiab]" in q and q.startswith("((PRAEZ") for q, _ in searched)
        # Erweitern eines alten Themas (100 Kandidaten): nur die neuen werden bewertet
        known = {p: {"x": 1} for p in ids[200:300]}
        prep2 = recommend.prepare_topic(None, {"title": "Systemtherapie"}, "Atopie", {}, "", plan=plan,
                                        window=["2025-07-01", "2026-10-31"], known=known)
        assert len(prep2["new"]) == 200
        # Thema mit 200 präzisen Treffern: 100 Kandidaten, keine Evidenzsuche
        searched.clear()
        pubmed.search = lambda q, n, e="": ([f"X{i}" for i in range(min(n, 200))], 200)
        prep3 = recommend.prepare_topic(None, {"title": "Klein"}, "Atopie", {}, "", plan=plan,
                                        window=["2025-07-01", "2026-10-31"])
        assert prep3["limit"] == 100 and prep3["evidence_hits"] is None and len(prep3["new"]) == 100
        # Projekt mit altem 100er-Thema: „Suche erweitern“ wird angeboten
        proj = pipeline.new_project()
        proj["topics"] = {"T1": {"id": "T1", "title": "Sys", "recommended": [], "cand_limit": 100,
                                 "n_candidates": 97, "query_hits": {"praezise": 1237, "breit": 2138}},
                          "T2": {"id": "T2", "title": "Klein", "recommended": [], "cand_limit": 100,
                                 "n_candidates": 95, "query_hits": {"praezise": 250, "breit": 600},
                                 "lead_first": recommend.LEAD_FIRST_VERSION}}
        assert pipeline.extension_pending(proj)["widen"] == ["T1"]
    finally:
        pubmed.search, pubmed.fetch_records = orig


def test_lead_journals_small_topics_new_guidelines():
    """Angelika 2026-11-23 (Vergleich Internisten-Update Angiologie): (3) kleine Themen vollständig bewerten,
    (4) führende Journale zuerst, auch im Sicherheitsnetz, (6) Leitlinien-Themen suchen gezielt neue Leitlinien
    im Suchzeitraum."""
    searched = []

    def fake_search(q, retmax, email=""):
        searched.append((q, retmax))
        if '"Eur Heart J"[ta]' in q:
            return [f"J{i}" for i in range(min(retmax, 150))], 150
        if "registry[tiab]" in q:
            return [f"E{i}" for i in range(min(retmax, 250))], 250
        if q.startswith("(PRAEZ"):
            return [f"P{i}" for i in range(min(retmax, PN[0]))], PN[0]
        return [f"B{i}" for i in range(min(retmax, 400))], 400

    PN = [5000]

    def rec(p):
        return {"pmid": p, "title": f"T {p}", "abstract": "a" * 400, "pub_types": [], "language": "eng"}

    orig = (pubmed.search, pubmed.fetch_records)
    pubmed.search = fake_search
    pubmed.fetch_records = lambda ids, email="": {i: rec(i) for i in ids}
    try:
        plan = {"pico": "p", "queries": [{"name": "praezise", "query": "PRAEZ"}, {"name": "breit", "query": "BREIT"}]}
        win = ["2025-10-01", "2026-10-31"]
        frame = {"journals": ["Vasa"]}
        # (4) sehr großes Thema: bis zu 100 aus führenden Journalen vor der Evidenz-Suche
        prep = recommend.prepare_topic(None, {"title": "Aneurysmen"}, "Angiologie", {}, "", plan=plan,
                                       window=win, frame=frame)
        ids = [c["pmid"] for c in prep["new"]]
        assert prep["limit"] == 300 and prep["lead_hits"] == 150
        assert ids[:100] == [f"J{i}" for i in range(100)] and ids[100:102] == ["E0", "E1"] and len(ids) == 300
        lq = next(q for q, _ in searched if '"Eur Heart J"[ta]' in q)
        assert '"Lancet Neurol"[ta]' in lq and '"Vasa"[ta]' in lq and lq.startswith("((PRAEZ")
        res = recommend.finish_topic(prep, {}, 3)
        assert res["lead_first"] == recommend.LEAD_FIRST_VERSION
        # breites Thema (100 Kandidaten): höchstens 30 aus führenden Journalen zuerst
        PN[0] = 200
        prep = recommend.prepare_topic(None, {"title": "Mittel"}, "Angiologie", {}, "", plan=plan, window=win)
        ids = [c["pmid"] for c in prep["new"]]
        assert prep["limit"] == 100 and ids[:30] == [f"J{i}" for i in range(30)] and "J30" not in ids
        # (3) kleines Thema mit 116 präzisen Treffern: alle 116 plus die übrigen Kandidaten des Reißverschlusses
        PN[0] = 116
        searched.clear()
        prep = recommend.prepare_topic(None, {"title": "Thrombophilie"}, "Angiologie", {}, "", plan=plan, window=win)
        ids = [c["pmid"] for c in prep["new"]]
        assert prep["limit"] >= 116 and prep["lead_hits"] is None and prep["evidence_hits"] is None
        assert {f"P{i}" for i in range(116)} <= set(ids) and len(ids) == len(set(ids))
        assert not any('"Eur Heart J"[ta]' in q for q, _ in searched), "kein Journal-Vorrang in kleinen Themen"
        res = recommend.finish_topic(prep, {}, 3)
        assert not res["lead_first"]
        # 60 präzise Treffer: unverändert 60 Kandidaten
        PN[0] = 50
        prep = recommend.prepare_topic(None, {"title": "Sehr klein"}, "Angiologie", {}, "", plan=plan, window=win)
        assert prep["limit"] == 60 and len(prep["new"]) == 60
    finally:
        pubmed.search, pubmed.fetch_records = orig

    # „Suche erweitern“: altes kleines Thema (60 von 116) und großes Thema ohne Journal-Vorrang
    proj = pipeline.new_project()
    proj["topics"] = {
        "T1": {"id": "T1", "title": "Thrombophilie", "recommended": [], "cand_limit": 60, "n_candidates": 51,
               "query_hits": {"praezise": 116, "breit": 181}},
        "T2": {"id": "T2", "title": "Aneurysmen", "recommended": [], "cand_limit": 300, "n_candidates": 286,
               "query_hits": {"praezise": 4634, "breit": 8292}},
        "T3": {"id": "T3", "title": "D-Dimere", "recommended": [], "cand_limit": 100, "n_candidates": 96,
               "query_hits": {"praezise": 155}, "lead_first": recommend.LEAD_FIRST_VERSION},
        "T4": {"id": "T4", "title": "Klein", "recommended": [], "cand_limit": 60, "n_candidates": 40,
               "query_hits": {"praezise": 56}}}
    assert pipeline.extension_pending(proj)["widen"] == ["T1", "T2"]

    # Sicherheitsnetz: führende internistische Journale in der Journal-Suche
    q = recommend.safety_queries({"anchor_query": "ANKER", "journals": ["Vasa"]})[1]["query"]
    assert all(f'"{j}"[ta]' in q for j in ("N Engl J Med", "Eur Heart J", "JAMA Cardiol", "Lancet Neurol", "Vasa"))
    assert pipeline.SAFETY_VERSION == 3

    # (6) Leitlinien-Themen: Suchzeitraum im Auftrag, mehr Websuchen; andere Themen unverändert
    from datetime import date
    job = guidelines.search_job("Neue AWMF-S3-Leitlinie zur Thrombose-Prophylaxe", "Angiologie", "g|T7",
                                window=(date(2025, 10, 1), date(2026, 10, 31)))
    assert "von 10/2025 bis 10/2026" in job.prompt and job.web_search == 12 and "höchstens 8" in job.prompt
    plain = guidelines.search_job("Dauer der Sekundärprophylaxe", "Angiologie", "g|T8")
    assert "NEUE Leitlinien" not in plain.prompt and plain.web_search == 8
    proj = pipeline.new_project()
    proj["search_window"] = ["2025-10-01", "2026-10-31"]
    old_gl = [{"title": "S3-Leitlinie pAVK", "register_number": "065-003", "text": "x" * 4000}]
    proj["topics"] = {
        "T1": {"id": "T1", "title": "Neue Leitlinien mit interdisziplinärem Bezug", "guidelines": old_gl},
        "T8": {"id": "T8", "title": "Dauer der Sekundärprophylaxe", "guidelines": []}}
    assert [tid for tid, _ in pipeline._guideline_todo(proj)] == ["T1"]
    assert pipeline.extension_pending(proj)["gl"] == ["T1"]
    assert pipeline._gl_window(proj, proj["topics"]["T1"]) == (date(2025, 10, 1), date(2026, 10, 31))
    assert pipeline._gl_window(proj, proj["topics"]["T8"]) is None
    calls = []
    o = (guidelines.search_guidelines, guidelines.fetch_guideline_text)
    guidelines.search_guidelines = lambda llm, topic, chapter, window=None: (
        calls.append(window) or [{"title": "S3-Leitlinie Rheuma kardiovaskulär", "register_number": "060-010"},
                                 {"title": "S3 pAVK", "register_number": "065 003"}])
    guidelines.fetch_guideline_text = lambda g: ("", "")
    try:
        pipeline.run_guideline_search(None, proj, lambda m: None)
    finally:
        guidelines.search_guidelines, guidelines.fetch_guideline_text = o
    gl = proj["topics"]["T1"]["guidelines"]
    assert calls == [(date(2025, 10, 1), date(2026, 10, 31))]
    assert [g["register_number"] for g in gl] == ["060-010", "065 003"], "gleiche Registernummer nicht doppelt"
    assert gl[1]["text"] == "x" * 4000, "schon geladener Volltext bleibt erhalten"
    assert proj["topics"]["T1"]["gl_new"] == guidelines.NEW_GL_VERSION
    assert pipeline._guideline_todo(proj) == [] and pipeline.extension_pending(proj)["gl"] == []


def test_safety_net_per_section():
    """Angelika 2026-11-15: eigene Suche je Hauptüberschrift (hohe Evidenz, Top-Journale, Suchbegriff allein)
    mit bis zu 100 neuen Kandidaten; Arbeiten mit Priorität A werden zugeordnet oder als neues Thema angeboten;
    gleiche Suche = keine neuen Kosten; ältere Projekte bekommen „Suche erweitern“ angeboten."""
    calls = {"sections": 0, "rank": [], "search": []}

    class L:
        def json(self, prompt, schema, **kw):
            props = schema["properties"]
            if "sections" in props:
                calls["sections"] += 1
                assert "8.1: Atopische Dermatitis" in prompt and "Atopische Dermatitis › Systemtherapie" in prompt
                return {"sections": [{"number": "8.1", "title_en": "atopic dermatitis", "query": "AD[tiab]"},
                                     {"number": "8.2", "title_en": "contact dermatitis", "query": "CD[tiab]"}]}
            if "items" in props:
                ids = re.findall(r"PMID: (\S+)", prompt)
                calls["rank"].append(("Atopische Dermatitis" if "Hauptabschnitts „Atopische Dermatitis“" in prompt
                                      else "Kontaktekzem" if "Hauptabschnitts „Kontaktekzem“" in prompt else "?"))
                return {"items": [{"pmid": i, "study_design": "RCT", "relevance": 90, "evidence": 90,
                                   "risk_of_bias": "Low", "novelty": 85, "practice_impact": 90 if i.endswith("0") else 40,
                                   "question_importance": 80, "journal_impact_factor": 10.0, "question_key": i,
                                   "is_primary_research": True, "rationale": "r"} for i in ids]}
            if "assignments" in props:
                keys = re.findall(r"^(\S+): ", prompt.split("Publikationen")[-1], re.MULTILINE)
                return {"assignments": [{"key": k, "topic_id": "T1" if k.startswith("AD") else ""} for k in keys]}
            raise AssertionError(list(props))

    def fake_search(q, retmax, email=""):
        calls["search"].append(q)
        tag = "AD" if "AD[tiab]" in q else "CD"
        ids = [f"{tag}{i}" for i in range(400)] + ["SHARED"]
        return ids[:retmax], 5000

    def rec(p):
        return {"pmid": p, "title": f"T {p}", "abstract": "a" * 400, "pub_types": [], "language": "eng",
                "journal": "J", "year": "2026"}

    orig = (pubmed.search, pubmed.fetch_records)
    pubmed.search = fake_search
    pubmed.fetch_records = lambda ids, email="": {i: rec(i) for i in ids}
    try:
        proj = pipeline.new_project()
        proj["email"] = "x@y.z"
        proj["toc"] = {"chapter_title": "Atopie und Ekzemerkrankungen"}
        proj["search_frame"] = {"anchor_query": "eczema[tiab]", "journals": ["Br J Dermatol"]}
        proj["topics"] = {
            "T1": {"id": "T1", "title": "Systemtherapie", "section_number": "8.1",
                   "section_title": "Atopische Dermatitis", "ranking": {"AD1": {"via": ""}}},
            "T2": {"id": "T2", "title": "Topika", "section_number": "8.1", "section_title": "Atopische Dermatitis"},
            "T3": {"id": "T3", "title": "Epikutantest", "section_number": "8.2", "section_title": "Kontaktekzem"},
            "T4": {"id": "T4", "title": "Beruf", "section_number": "8.2", "section_title": "Kontaktekzem"}}
        proj["safety_net"] = {"key": "alt", "items": {}}  # Stand bis 2026-11-14: eine Suche für das Kapitel
        assert pipeline.extension_pending(proj)["net"] == 2
        pipeline.run_safety_net(L(), proj, lambda *a: None)
        net = proj["safety_net"]
        assert calls["sections"] == 1 and set(net["sections"]) == {"8.1", "8.2"}
        assert any(q.startswith("((AD[tiab]) AND (randomized") for q in calls["search"])
        assert any("\"Br J Dermatol\"[ta]" in q and "CD[tiab]" in q for q in calls["search"])
        assert net["sections"]["8.1"]["n_new"] == 100 and net["sections"]["8.2"]["n_new"] == 100
        assert "AD1" not in net["sections"]["8.1"]["items"] and "AD0" in net["sections"]["8.1"]["items"], "AD1 schon im Thema bewertet"
        # A (Praxisrelevanz 90) bei PMIDs auf 0: AD-Arbeiten ins Thema T1, CD-Arbeiten als neues Thema
        assert "AD10" in proj["topics"]["T1"]["ranking"] and proj["topics"]["T1"]["ranking"]["AD10"]["via"] == "Sicherheitsnetz"
        assert "CD10" in proj["topics"][pipeline.SAFETY]["ranking"] and net["orphans"] > 0
        assert any("Atopische Dermatitis" in r for r in calls["rank"]) and any("Kontaktekzem" in r for r in calls["rank"])
        assert pipeline.extension_pending(proj)["net"] == 0
        n_rank = len(calls["rank"])
        pipeline.run_safety_net(L(), proj, lambda *a: None)  # gleiche Suche: gemerkt, keine Kosten
        assert len(calls["rank"]) == n_rank and calls["sections"] == 1
        # Ein Kapitel ohne Gliederung in Hauptabschnitte mit mehreren Themen: eine Suche mit dem Kapitel-Anker
        proj2 = {"topics": {"A": {"title": "a", "section_number": "1"}, "B": {"title": "b", "section_number": "2"}}}
        assert pipeline.safety_sections(proj2) == []
    finally:
        pubmed.search, pubmed.fetch_records = orig


def test_oe_list():
    """Angelika 2026-11-15: optionale Publikationsliste aus OpenEvidence. Schon gefundene Arbeiten werden markiert,
    neue im passenden Thema bewertet, ohne passendes Thema unter „Neue Themen“; nicht auffindbare werden genannt;
    nach einer neuen Suche bleibt alles ohne Kosten erhalten; eine neue Liste ersetzt die alte."""
    calls = {"parse": 0, "rank": []}

    class L:
        def json(self, prompt, schema, **kw):
            props = schema["properties"]
            if "entries" in props:
                calls["parse"] += 1
                assert "OpenEvidence" in prompt
                if "ZWEITE" in prompt:
                    return {"entries": [{"title": "Neue Studie B", "first_author": "", "journal": "", "year": "",
                                         "doi": "", "pmid": "502"}]}
                return {"entries": [
                    {"title": "Schon gefunden", "first_author": "A", "journal": "J", "year": "2026", "doi": "", "pmid": "100"},
                    {"title": "Neue Studie A", "first_author": "B", "journal": "J", "year": "2026", "doi": "10.1/x", "pmid": ""},
                    {"title": "Ganz anderes Thema", "first_author": "C", "journal": "J", "year": "2026", "doi": "", "pmid": "503"},
                    {"title": "Gibt es nicht", "first_author": "D", "journal": "J", "year": "2026", "doi": "", "pmid": ""}]}
            if "assignments" in props:
                return {"assignments": [{"key": "501", "topic_id": "T1"}, {"key": "502", "topic_id": "T1"},
                                        {"key": "503", "topic_id": ""}]}
            if "items" in props:
                ids = re.findall(r"PMID: (\d+)", prompt)
                calls["rank"].append(ids)
                return {"items": [{"pmid": i, "study_design": "RCT", "relevance": 90, "evidence": 90,
                                   "risk_of_bias": "Low", "novelty": 85, "practice_impact": 90,
                                   "question_importance": 80, "journal_impact_factor": 10.0, "question_key": i,
                                   "is_primary_research": True, "rationale": "r"} for i in ids]}
            raise AssertionError(list(props))

    def rec(p):
        return {"pmid": p, "title": f"T {p}", "abstract": "a" * 400, "pub_types": [], "language": "eng",
                "journal": "J", "year": "2026", "authors": ["X Y"]}

    orig = (pubmed.search, pubmed.fetch_records, pubmed.find_pmid)
    pubmed.find_pmid = lambda doi="", title="", email="": "501" if doi == "10.1/x" else None
    pubmed.fetch_records = lambda ids, email="": {i: rec(i) for i in ids}
    try:
        proj = pipeline.new_project()
        proj["email"] = "x@y.z"
        proj["toc"] = {"chapter_title": "Atopie"}
        it = {"score": 80, "priority": "A", "relevance": 90, "evidence": 80, "practice_impact": 80, "novelty": 60,
              "risk_of_bias": "Low", "question_importance": 70, "journal_impact_factor": 5, "is_primary_research": True,
              "question_key": "q", "study_design": "RCT", "rationale": "r"}
        proj["topics"] = {"T1": {"id": "T1", "title": "Systemtherapie", "plan": {"pico": "P"},
                                 "ranking": {"100": dict(it)}, "recommended": ["100"], "reserve": []},
                          "T2": {"id": "T2", "title": "Topika", "ranking": {}, "recommended": [], "reserve": []}}
        proj["records"] = {"100": rec("100")}
        pipeline.run_oe_list(L(), proj, "Antwort von OpenEvidence …", "oe.docx", lambda *a: None)
        oe = proj["oe_list"]
        assert oe["n_entries"] == 4 and set(oe["items"]) == {"100", "501", "503"}
        assert oe["items"]["100"]["found"] and oe["items"]["100"]["topic"] == "T1"
        assert len(oe["missing"]) == 1 and "Gibt es nicht" in oe["missing"][0]
        t1 = proj["topics"]["T1"]
        assert t1["ranking"]["100"].get("oe") is True
        assert t1["ranking"]["501"]["via"] == "OpenEvidence" and t1["ranking"]["501"]["priority"] == "A"
        neu = proj["topics"][pipeline.SAFETY]
        assert "503" in neu["ranking"] and neu["enabled"] is False
        assert sorted(sum(calls["rank"], [])) == ["501", "503"], "schon gefundene Arbeiten werden nicht neu bewertet"
        assert pipeline.oe_pmids(proj) == {"100", "501", "503"}
        # Neue Suche ersetzt die Rangliste des Themas: die Liste wird ohne Kosten wieder eingesetzt
        n_rank = len(calls["rank"])
        t1["ranking"] = {"100": dict(it)}
        pipeline.apply_oe_list(proj)
        assert "501" in t1["ranking"] and t1["ranking"]["100"]["oe"] and len(calls["rank"]) == n_rank
        # Eine zweite Liste ersetzt die erste
        pipeline.run_oe_list(L(), proj, "ZWEITE Liste von OpenEvidence", "eingefügter Text", lambda *a: None)
        assert set(proj["oe_list"]["items"]) == {"502"}
        assert "501" not in t1["ranking"] and "502" in t1["ranking"] and not t1["ranking"]["100"].get("oe")
        assert pipeline.SAFETY not in proj["topics"], "ohne Arbeiten ohne Thema entfällt „Neue Themen“"
        # Auswahlliste der Autoren: Arbeiten der Liste stehen dort auch ohne Empfehlung und ohne Reserve
        from medupdate import selection_sheet
        for k in ("recommended", "reserve"):
            t1[k] = [p for p in t1.get(k, []) if p != "502"]
        t1.pop("selected", None)
        items = {i["id"]: i for tp in selection_sheet.sheet_data(proj)["topics"] for i in tp["items"]}
        assert items["502"]["status"] == "OpenEvidence" and not items["502"]["checked"]
    finally:
        pubmed.search, pubmed.fetch_records, pubmed.find_pmid = orig


def test_focus_followup_window():
    """Angelika 2026-11-10 (Validierung an drei Kapiteln): Schalter „Interventionelle Studien abwerten“,
    Nachsuche nach Folgepublikationen großer Studien, Warnung beim Suchzeitraum."""
    from datetime import date
    def it(rel, ev, imp, key=""):
        return {"relevance": rel, "evidence": ev, "practice_impact": imp, "novelty": 60, "risk_of_bias": "Low",
                "question_importance": 70, "journal_impact_factor": 5, "is_primary_research": True,
                "question_key": key, "study_design": "RCT", "rationale": "r"}
    proj = pipeline.new_project()
    proj["toc"] = {"chapter_title": "pAVK konservativ"}
    proj["email"] = "x@example.org"
    proj["search_window"] = ["2025-03-01", "2026-03-31"]
    proj["topics"] = {"T1": {"id": "T1", "title": "Prognose", "number": "8.1", "ranking": {
        "1": it(90, 90, 80, "a"), "2": it(88, 85, 80, "b"), "3": it(80, 60, 50, "c")}}}
    proj["records"] = {p: {"pmid": p, "title": f"Titel {p}", "authors": ["Muster A"], "abstract": "x " * 200,
                           "journal": "J", "year": "2025", "pub_types": [], "language": "eng"} for p in "123"}
    calls = []

    class L:
        def json(self, prompt, schema, **kw):
            props = schema.get("properties", {})
            calls.append(prompt[:30])
            if "assignments" in props:
                return {"assignments": [{"key": "9", "topic_id": "T1"}]}
            sub = props.get("items", {}).get("items", {}).get("properties", {})
            if "invasive" in sub:
                return {"items": [{"key": "2", "invasive": True}, {"key": "1", "invasive": False},
                                  {"key": "3", "invasive": False}]}
            if "trial" in sub:
                return {"items": [{"key": "1", "trial": "BEST-CLI"}, {"key": "2", "trial": ""}]}
            return {"items": [{**it(85, 70, 60, "d"), "pmid": "9"}]}
    # Schalter aus: nichts eingeordnet, Arbeit 2 empfohlen
    assert pipeline.classify_invasive(L(), proj) == 0 and not calls
    pipeline.select_chapter(proj)
    assert "2" in proj["topics"]["T1"]["recommended"]
    proj["focus_conservative"] = True
    assert pipeline.classify_invasive(L(), proj) == 1
    pipeline.select_chapter(proj)
    t = proj["topics"]["T1"]
    assert "2" not in t["recommended"] and "2" in t["reserve"] and t["reserve"][-1] == "2", (t["recommended"], t["reserve"])
    n = len(calls)
    assert pipeline.classify_invasive(L(), proj) == 0 and len(calls) == n  # gemerkt
    # Nachsuche: Studienname -> PubMed -> neue Arbeit 9 wird zugeordnet und bewertet
    old_search, old_fetch = pipeline.pubmed.search, pipeline.pubmed.fetch_records
    searched = []
    pipeline.pubmed.search = lambda q, n, e="": (searched.append(q) or (["9", "1"], 2))
    pipeline.pubmed.fetch_records = lambda ids, e="": {"9": {"pmid": "9", "title": "BEST-CLI follow-up",
        "authors": ["Neu B"], "abstract": "y " * 200, "journal": "J", "year": "2025", "pub_types": [],
        "language": "eng"}}
    try:
        assert pipeline.followup_pending(proj) == 3
        pipeline.run_followup(L(), proj, lambda *a: None)
        assert '"BEST-CLI"[tiab]' in searched[0] and len(searched) == 1
        assert t["ranking"]["9"]["via"] == "Folgepublikation" and "9" in proj["records"]
        assert pipeline.followup_pending(proj) == 0
        n = len(calls)
        pipeline.run_followup(L(), proj, lambda *a: None)  # nichts Neues: keine KI-Kosten
        assert len(calls) == n and len(searched) == 1
    finally:
        pipeline.pubmed.search, pipeline.pubmed.fetch_records = old_search, old_fetch
    # Warnung beim Suchzeitraum
    today = date(2026, 11, 10)
    assert recommend.window_warning(["2025-03-01", "2026-03-31"], ["15 VTE 2025.pdf"], today) == ""
    assert "Zukunft" in recommend.window_warning(["2025-11-01", "2026-11-30"], ["15 VTE 2025.pdf"], today)
    assert "2025" in recommend.window_warning(["2026-03-01", "2027-03-31"], ["15 VTE 2025.pdf"], date(2027, 6, 1))
    assert recommend.window_warning(None, ["x 2025.pdf"], today) == ""


def test_toc_merge():
    """Schritt 0: Inhaltsverzeichnisse mehrerer Jahrgänge je Thema zusammenführen; Hot Topics und Einzelkapitel
    unverändert, Nummern wie im neuesten Jahrgang, Dateinamen mit beiden Autoren beim Autorenwechsel."""
    from medupdate import toc_merge as M
    calls = []
    books = {
        "HB_2024.txt": {"handbook": "Angio Update", "year": "", "chapters": [
            {"number": "1", "title": "Ultraschall", "authors": ["Czihal"], "hot_topic": False,
             "items": [{"level": 1, "title": "Supraaortale Arterien"}, {"level": 2, "title": "Paragangliome"},
                       {"level": 1, "title": "Literatur"}]},
            {"number": "2", "title": "Hot Topic: Diabetischer Fuß", "authors": ["Spraul"], "hot_topic": True,
             "items": [{"level": 1, "title": "Einleitung"}]}]},
        "HB_2026.txt": {"handbook": "Angio Update", "year": "2026", "chapters": [
            {"number": "1", "title": "Vaskuläre Funktionsdiagnostik und Ultraschall", "authors": ["Czihal", "Neu"],
             "hot_topic": False, "items": [{"level": 1, "title": "Supraaortale Arterien"},
                                           {"level": 2, "title": "Ultraschallmikroskopie"}]}]}}

    class L:
        def json(self, prompt, schema, **kw):
            props = schema["properties"]
            calls.append(list(props))
            if "chapters" in props:
                name = re.search(r"Datei „([^“]+)“", prompt).group(1)
                assert "ROHGLIEDERUNG" in prompt
                return json.loads(json.dumps(books[name]))
            if "groups" in props:
                assert "B0C0 | 2024 | Ultraschall" in prompt
                return {"groups": [{"title": "Vaskuläre Funktionsdiagnostik und Ultraschall", "hot_topic": False,
                                    "members": ["B0C0", "B1C0"]},
                                   {"title": "Hot Topic: Diabetischer Fuß", "hot_topic": True, "members": ["B0C1"]}]}
            if props.keys() == {"items"}:
                assert "JAHRGANG 2024" in prompt and "JAHRGANG 2026" in prompt and "Literatur" not in \
                    prompt.split("JAHRGANG 2024")[1].split("JAHRGANG 2026")[0]
                return {"items": [{"level": 1, "title": "Supraaortale Arterien"},
                                  {"level": 2, "title": "Ultraschallmikroskopie"}, {"level": 2, "title": "Paragangliome"}]}
            raise AssertionError(list(props))

    proj = pipeline.new_project()
    files = [("HB_2024.txt", b"1 ULTRASCHALL\n1.1 Supraaortale Arterien"), ("HB_2026.txt", b"1 X\n1.1 Y")]
    M.run_toc_merge(L(), proj, files, "", lambda *a: None)
    st = proj["toc_merge"]
    assert [c["title"] for c in st["chapters"]] == ["Vaskuläre Funktionsdiagnostik und Ultraschall",
                                                     "Hot Topic: Diabetischer Fuß"]
    ch, ht = st["chapters"]
    assert ch["number"] == "1" and ht["number"] == "2" and ht["how"] == "unverändert übernommen"
    assert M.as_text(ch).splitlines()[1:] == ["1.1 Supraaortale Arterien", "  1.1.1 Ultraschallmikroskopie",
                                              "  1.1.2 Paragangliome"]
    assert M.filename(ch, st["handbook"]) == ("Inhaltsverzeichnis Angio Update 2024_2026_Vaskuläre "
                                              "Funktionsdiagnostik und Ultraschall_Czihal_Neu.docx")
    assert M.filename(ht, st["handbook"]) == "Inhaltsverzeichnis Angio Update 2024_Hot Topic Diabetischer Fuß_Spraul.docx"
    with zipfile.ZipFile(io.BytesIO(M.zip_all(st))) as z:
        assert len(z.namelist()) == 2
    n = len(calls)
    M.run_toc_merge(L(), proj, files, "", lambda *a: None)  # alles zwischengespeichert: keine neuen KI-Kosten
    assert len(calls) == n
    # Die Rohgliederung einer Text-Datei filtert Literaturangaben und fügt umbrochene Überschriften zusammen
    raw = ("x\n" * 80000 + "2 HOT TOPIC: HANDBESCHWERDEN –\nDIFFERENTIALDIAGNOSEN\n2.1 Einleitung\n"
           "5 Chen SP, Zhong Z, Tu DP. Subtypes of flow\n22 Jahren publiziert.\n").encode()
    t = M.outline_text("a.txt", raw)
    assert "2 HOT TOPIC: HANDBESCHWERDEN – DIFFERENTIALDIAGNOSEN" in t and "Chen SP" not in t and "22 Jahren" not in t



def test_final_toc_rules():
    """Entscheidungen der Autorin 2026-11-13: Neue Themen vor Varia am Ende, Schlagwort-Überschrift je
    Arbeit (gleiche Fragestellung = gleiche Überschrift), höchstens drei Ebenen im Inhaltsverzeichnis."""
    proj = {"topics": {"VARIA": {"title": "Varia"}, "A": {"title": "A"}, "NEU": {"title": "Neu", "safety": True},
                       "B": {"title": "B"}}, "pubs": {}, "records": {"1": {"title": "GLP-1 before EGD"},
                       "2": {"title": "Holding GLP-1"}, "3": {"title": "AI quality EGD"}}}
    assert pipeline.topic_order(proj) == ["A", "B", "NEU", "VARIA"]

    class L:
        calls = 0
        def json(self, prompt, schema, **kw):
            L.calls += 1
            assert "headings" in schema["properties"] and "GENAU dieselbe" in prompt
            h = {"0": "GLP-1-Agonisten vor der ÖGD", "1": "KI in der ÖGD", "2": "GLP-1-Agonisten vor der ÖGD"}
            return {"headings": [{"id": i, "de": d, "en": d} for i, d in h.items()]}
    g = pipeline.keyword_headings(L(), proj, "VARIA", ["1", "3", "2"])
    assert [x[0] for x in g] == ["GLP-1-Agonisten vor der ÖGD", "KI in der ÖGD"] and g[0][2] == ["1", "2"]
    pipeline.keyword_headings(L(), proj, "VARIA", ["1", "3", "2"])
    assert L.calls == 1  # Cache

    from medupdate.manuscript import build_docx
    from docx import Document
    import io
    blocks = [{"topic": "Thema tief", "section": ("5.1", "Ösophagus"), "subsection": ("5.1.2", "Barrett"),
               "number": "5.1.2.1", "level": 3, "pubs": []},
              {"topic": "Thema flach", "section": None, "subsection": None, "number": "5.1.3", "level": 2, "pubs": []}]
    doc = Document(io.BytesIO(build_docx("Kapitel", blocks, "Deutsch")))
    texts = [p.text for p in doc.paragraphs]
    toc = texts[texts.index("Inhaltsverzeichnis"):texts.index("Inhaltsverzeichnis") + 6]
    assert "5.1.2\tBarrett" in toc and "5.1.3\tThema flach" in toc and not any(t.startswith("5.1.2.1") for t in toc)
    assert any("Thema tief" in t for t in texts)  # im Text weiterhin vorhanden

def test_guideline_topic():
    """Angelika 2026-11-17 (Vergleich Akne/Rosazea): Überschriften, die eine Leitlinie nennen, bekommen Leitlinien
    und Konsensuspapiere als Hauptpublikation; Primärstudien dieses Themas nur in der Reserve."""
    def it(rel, sc_ev, primary=True, design="RCT", key=""):
        return {"relevance": rel, "evidence": sc_ev, "practice_impact": 80, "novelty": 70, "risk_of_bias": "Low",
                "question_importance": 80, "journal_impact_factor": 6, "is_primary_research": primary,
                "question_key": key, "study_design": design, "rationale": "r"}
    assert pipeline.is_guideline_topic({"title": "Die neue S2k-Leitlinie Therapie der Akne"})
    assert pipeline.is_guideline_topic({"title": "EuroGuiDerm-Update"})
    assert not pipeline.is_guideline_topic({"title": "Topische Therapie", "english_search_focus": "guideline"})
    proj = pipeline.new_project()
    proj["toc"] = {"chapter_title": "Akne"}
    proj["manual_limits"] = {"publications": 10}
    pipeline.apply_manual_limits(proj)
    proj["topics"] = {
        "T1": {"id": "T1", "title": "Systemische Therapie", "ranking": {
            "G": it(95, 80, primary=False, design="Evidenzbasierte Leitlinie"), "S1": it(90, 85, key="s1")}},
        "T2": {"id": "T2", "title": "Die neue S2k-Leitlinie Therapie der Akne", "ranking": {
            "G": it(95, 80, primary=False, design="Evidenzbasierte Leitlinie"),
            "N": it(70, 70, primary=False, design="Narratives Review"),
            "P": it(60, 80, primary=False, design="Übersicht", key="p"),
            "S2": it(80, 85, key="s2")}},
    }
    proj["records"] = {p: {"pmid": p, "title": f"Titel {p}", "authors": ["Muster A"], "abstract": "x",
                           "journal": "J", "year": "2026", "pub_types": ["Practice Guideline"] if p == "P" else []}
                       for p in ("G", "N", "P", "S1", "S2")}
    pipeline.select_chapter(proj)
    t1, t2 = proj["topics"]["T1"], proj["topics"]["T2"]
    assert set(t2["recommended"]) == {"G", "P"}, t2["recommended"]  # Leitlinien statt Primärstudie
    assert "S2" in t2["reserve"] and "S2" not in t1["recommended"]  # Primärstudie nur Reserve
    assert "G" not in t1["recommended"] + t1["background"] and "S1" in t1["recommended"]
    assert "N" not in t2["recommended"]  # narrative Übersicht bleibt Hintergrund
    assert "N" in t2["background"] and "G" not in t2["background"]
    # ohne Leitlinien-Überschrift bleibt alles wie bisher
    proj["topics"]["T2"]["title"] = "Neue Studien"
    for t in proj["topics"].values():
        t.pop("selected", None)
    pipeline.select_chapter(proj)
    assert "G" not in proj["topics"]["T2"]["recommended"] and "S2" in proj["topics"]["T2"]["recommended"]


def test_ai_notice_without_template():
    from medupdate import formal
    blocks = [{"number": "8.1", "topic": "Thema", "level": 1, "pubs": []}]
    raw = manuscript.build_docx("Kapitel", blocks, "Deutsch")
    for lang in ("Deutsch", "Englisch"):
        _, final = formal.run_formal_check(raw, None, None, lang=lang)
        doc = Document(io.BytesIO(final))
        texts = [p.text for p in doc.paragraphs]
        note = docx_tools.AI_NOTICE[lang]
        ik = texts.index("Interessenskonflikte")
        assert texts.count(note) == 1 and ik < texts.index(note), "KI-Hinweis unter den Interessenkonflikten"
        brk = next(i for i, p in enumerate(doc.paragraphs) if docx_tools._breaks_page(p))
        assert texts.index(note) < brk, "KI-Hinweis auf Seite 1"
        assert all(not t for t in texts[ik + 1:texts.index(note)]), "Platz zum Ausfüllen bleibt frei"
        assert note.count(".") == 1


def test_length_plan_topic_coverage():
    """Angelika 2026-11-17 (Vergleich Onkologie unterer GI-Trakt): Viele Spitzenstudien dürfen nicht das ganze
    Seitenbudget belegen; jedes Thema mit A- oder B-Arbeit bleibt im Plan besetzt."""
    from collections import Counter
    from medupdate.appraisal import plan_tiers
    counts = {"long": 3, "mid": 8, "short": 20, "news": 19}
    # 21 Spitzenstudien in Thema A, dazu schwächere Arbeiten in B und C
    pubs = [{"id": f"a{i}", "length_score": 95 - i * 0.5, "topic": "A"} for i in range(21)]
    pubs += [{"id": f"x{i}", "length_score": 70 - i * 0.3, "topic": "A"} for i in range(27)]
    pubs += [{"id": "b1", "length_score": 55, "topic": "B"}, {"id": "c1", "length_score": 50, "topic": "C"}]
    tiers, overflow = plan_tiers(pubs, {"tier_counts": counts, "max_pages": 28}, 6)
    assert Counter(tiers.values())["1 Seite (Lang)"] == 6
    assert "b1" in tiers and "c1" in tiers, "die einzige Arbeit eines Themas fällt nicht wegen der Länge heraus"
    assert overflow and all(p.startswith(("a", "x")) for p in overflow)
    # ohne Themenangabe wie bisher: die schwächste fällt zuerst
    tiers2, overflow2 = plan_tiers([{k: v for k, v in p.items() if k != "topic"} for p in pubs],
                                   {"tier_counts": counts, "max_pages": 28}, 6)
    assert "c1" in overflow2
    # Kapitelauswahl: Themen mit nur B-Arbeiten bekommen ihre beste Arbeit, auch wenn die Zielzahl knapp ist
    def it(rel, ev, pi=80):
        return {"relevance": rel, "evidence": ev, "practice_impact": pi, "novelty": 70, "risk_of_bias": "Low",
                "question_importance": 80, "journal_impact_factor": 6, "is_primary_research": True,
                "question_key": "", "study_design": "RCT", "rationale": "r"}
    proj = pipeline.new_project()
    proj["toc"] = {"chapter_title": "Onkologie"}
    proj["manual_limits"] = {"publications": 3}
    pipeline.apply_manual_limits(proj)
    proj["topics"] = {
        "T1": {"id": "T1", "title": "Erstlinie", "ranking": {f"A{i}": it(95, 95) for i in range(5)}},
        "T2": {"id": "T2", "title": "TNT", "ranking": {"B1": it(90, 60, pi=55)}},
    }
    proj["records"] = {p: {"pmid": p, "title": p, "authors": ["M A"], "abstract": "x", "journal": "J",
                           "year": "2025", "pub_types": []} for t in proj["topics"].values() for p in t["ranking"]}
    pipeline.select_chapter(proj)
    assert proj["topics"]["T2"]["recommended"] == ["B1"], proj["topics"]["T2"]
    assert len(proj["topics"]["T1"]["recommended"]) == 2


def test_heading_parts():
    """Angelika 2026-11-17: zusammengesetzte Überschriften bekommen je Teil eine Teilsuche mit Zusatzkandidaten."""
    hp = recommend.heading_parts
    assert hp("Chirurgie, Strahlentherapie und neoadjuvante Chemotherapie") == [
        "Chirurgie", "Strahlentherapie", "neoadjuvante Chemotherapie"]
    assert hp("Oberflächenthrombose, Varikose, CVI") == ["Oberflächenthrombose", "Varikose", "CVI"]
    assert hp("Kolorektales Karzinom: Prävention und Pathogenese") == ["Prävention", "Pathogenese"]
    assert hp("TNT") == [] and hp("Adjuvanz bei MMRd-Tumoren") == [] and hp("Early-Onset-CRC") == []
    assert hp("Primär- und Sekundärprävention") == []
    assert hp("A, B, C, D, E") == [], "lange Aufzählungen: keine Teilsuchen"

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def json(self, prompt, schema, **kw):
            self.calls.append((prompt, schema))
            data = {"pico": "p", "exclusion": "", "queries": [
                {"name": "praezise", "query": "rectal cancer AND radiotherapy"},
                {"name": "breit", "query": "rectal cancer"}, {"name": "hohe_evidenz", "query": "x"}]}
            if "teilsuchen" in schema["properties"]:
                data["teilsuchen"] = [{"teil": "Chirurgie", "query": "rectal cancer AND robotic"},
                                      {"teil": "Strahlentherapie", "query": "rectal cancer AND radiotherapy"},
                                      {"teil": "neoadjuvante Chemotherapie", "query": "rectal cancer AND neoadjuvant"},
                                      {"teil": "zu viel", "query": "y"}]
            if "queries" not in schema["properties"]:
                data.pop("queries")
            return data

    from datetime import date
    llm = FakeLLM()
    topic = {"id": "T8", "title": "Chirurgie, Strahlentherapie und neoadjuvante Chemotherapie"}
    plan = recommend.plan_search(llm, topic, "Rektumkarzinom", date(2025, 1, 1), date(2026, 1, 1))
    names = [q["name"] for q in plan["queries"]]
    assert names[:3] == ["praezise", "breit", "hohe_evidenz"] and len(names) == 6
    assert names[3] == "teil: Chirurgie" and "teilsuchen" not in plan
    assert "Teilen zusammen" in llm.calls[0][0]
    plan2 = recommend.plan_search(llm, {"id": "T9", "title": "TNT"}, "Rektum", date(2025, 1, 1), date(2026, 1, 1))
    assert len(plan2["queries"]) == 3 and "teilsuchen" not in llm.calls[-1][1]["properties"]
    # Teilsuchen zählen nicht für die Kandidatengrenze
    assert recommend.candidate_limit({"praezise": 100, "teil: Chirurgie": 5000}) == recommend.candidate_limit(
        {"praezise": 100})
    # Zusatzkandidaten: je Teil bis zu PART_EXTRA, hohe Evidenz zuerst, ohne Doppelte
    old_search = pubmed.search
    try:
        def fake_search(q, n, email=""):
            base = "R" if "robotic" in q else "N" if "neoadjuvant" in q else "S"
            ids = [f"{base}{i}" for i in range(200)]
            if "randomized controlled trial[pt]" in q:
                ids = [f"{base}E{i}" for i in range(10)]
            return ids[:n], len(ids)
        pubmed.search = fake_search
        pmids, stats = recommend.part_extras(plan, ["S0", "S1"], date(2025, 1, 1), date(2026, 1, 1), "")
        assert pmids[:2] == ["S0", "S1"] and "RE0" in pmids and pmids.index("RE0") < pmids.index("R0")
        assert len(pmids) == 2 + recommend.PART_EXTRA_MAX and len(set(pmids)) == len(pmids)
        assert stats["teil: Chirurgie (hohe Evidenz)"] == 10
    finally:
        pubmed.search = old_search
    # älteres Projekt: Teilsuchen nachträglich, dann nur neue Kandidaten bewerten
    t = {"id": "T8", "title": topic["title"], "plan": {"queries": [{"name": "praezise", "query": "q"}]},
         "recommended": [], "ranking": {}}
    assert pipeline.parts_pending(t)
    proj = pipeline.new_project()
    proj["toc"] = {"chapter_title": "Rektum"}
    proj["topics"] = {"T8": t, "T9": {"id": "T9", "title": "TNT", "plan": {"queries": [{"name": "praezise",
                                                                                          "query": "q"}]}}}
    assert pipeline.extension_pending(proj)["parts"] == ["T8"]
    assert pipeline.add_parts(llm, proj, lambda m: None) == ["T8"]
    assert t["parts_new"] and t["parts_done"] and len(recommend.part_queries(t["plan"])) == 3
    assert not pipeline.parts_pending(t) and pipeline.extension_pending(proj)["parts"] == []


if __name__ == "__main__":
    test_length_plan_topic_coverage()
    test_heading_parts()
    test_fazit_and_headings()
    test_final_toc_rules()
    test_direct_upload()
    test_enforce_pub_limit()
    test_reassign_topics()
    test_kurznachrichten_block()
    test_llm_retry_and_cost()
    test_results_saved_before_abort()
    test_search_broadening()
    test_length_budget()
    test_outline_levels()
    test_repeated_citations()
    test_split_intro()
    test_manual_limits()
    test_manual_window()
    test_sota_choice()
    test_transition_slots()
    test_selection_package()
    test_guideline_needs()
    test_toc_merge()
    test_chapter_selection()
    test_guideline_topic()
    test_huge_topics_evidence_first()
    test_lead_journals_small_topics_new_guidelines()
    test_safety_net_per_section()
    test_oe_list()
    test_focus_followup_window()
    test_search_plan_and_safety_net()
    test_sota_trailing_note()
    test_full_pipeline()
    test_ai_notice_without_template()
    test_comment_box()
    test_compact_note()
    print("OK")
