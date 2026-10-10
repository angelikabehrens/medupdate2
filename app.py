"""MyLuo Pro – nahezu automatisierte Erstellung eines wissenschaftlichen Fortbildungsmanuskripts.

Zwei Schritte für die Nutzerin bzw. den Nutzer:
1. Inhaltsverzeichnis, Formatvorlage und Hinweise für Autoren hochladen → KI-gestützte
   Literaturempfehlung und Leitlinienrecherche.
2. Volltexte, eigene frühere Manuskripte und ggf. fehlende Leitlinien hochladen → die KI erstellt
   kritische Würdigungen, Kürzungen, State of the Art, Übersetzung, Stilanpassung,
   Literaturverzeichnis, Formatierung und formale Prüfung.
"""

import hmac
import time
from datetime import date

import streamlit as st

from medupdate import pipeline
from medupdate.config import secret
from medupdate.files import extract_text
from medupdate.llm import LLM

st.set_page_config(page_title="MyLuo Pro", layout="wide")
st.markdown(
    "<div style='font-size:3rem; font-weight:700; line-height:1.2;'>MyLuo Pro</div>"
    "<div style='font-size:1.3rem; font-style:italic; margin-top:0.2rem;'>Intelligent Scientific Workflow: "
    "Beyond Summaries. Into Scientific Synthesis.</div><div style='margin-top:2rem;'></div>",
    unsafe_allow_html=True,
)

# ---------- Zugangscode (wie in MyLuo) ----------
_APP_PASSWORD = secret("APP_PASSWORD")
if _APP_PASSWORD and not st.session_state.get("_access_ok"):
    with st.form("access_form"):
        _code = st.text_input("Zugangscode", type="password")
        _submitted = st.form_submit_button("Weiter")
    if _submitted:
        if hmac.compare_digest(_code.encode("utf-8"), _APP_PASSWORD.encode("utf-8")):
            st.session_state["_access_ok"] = True
            st.rerun()
        else:
            time.sleep(1.5)
            st.error("Der Zugangscode ist nicht korrekt.")
    st.stop()

if "proj" not in st.session_state:
    st.session_state["proj"] = pipeline.new_project()
proj = st.session_state["proj"]

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# ---------- Seitenleiste ----------
_SHARED_KEY = secret("ANTHROPIC_API_KEY")
with st.sidebar:
    if _SHARED_KEY:
        api_key = _SHARED_KEY
        st.header("🔑 KI-Zugang")
        st.caption("Die KI-Funktionen laufen über den gemeinsamen Zugang dieser App.")
    else:
        st.header("🔑 KI-Zugang")
        api_key = st.text_input("Anthropic-API-Schlüssel", type="password")
    if proj.get("cost_usd"):
        st.caption(f"KI-Kosten dieses Projekts bisher ≈ {proj['cost_usd']:.2f} USD")
    st.divider()
    st.header("💾 Zwischenstand")
    st.caption("Die App speichert nichts auf dem Server. Den Zwischenstand hier herunterladen und später "
               "wieder laden, um weiterzuarbeiten.")
    if st.button("💾 Zwischenstand vorbereiten"):
        st.session_state["_export"] = pipeline.export_project(proj)
    if st.session_state.get("_export"):
        st.download_button("⬇️ Zwischenstand herunterladen", data=st.session_state["_export"],
                           file_name=f"medupdate2_zwischenstand_{date.today().isoformat()}.json.gz",
                           mime="application/gzip", on_click=lambda: st.session_state.pop("_export", None))
    _load = st.file_uploader("Zwischenstand laden", type=["gz", "json"], key="load_state")
    if _load is not None and st.button("Laden"):
        st.session_state["proj"] = pipeline.import_project(_load.read())
        st.rerun()
    if st.button("🗑️ Neues Projekt beginnen"):
        st.session_state["proj"] = pipeline.new_project()
        st.rerun()

with st.expander("👋 So funktioniert MyLuo Pro", expanded=not proj.get("toc")):
    st.markdown(
        "**Schritt 1:** E-Mail-Adresse eingeben, Inhaltsverzeichnis, Formatvorlage und Hinweise für Autoren "
        "hochladen. Die KI zerlegt das Inhaltsverzeichnis in Themen, sucht in PubMed die wichtigsten neuen "
        "Publikationen und recherchiert die aktuellen Leitlinien.\n\n"
        "**Schritt 2:** Die Originalpublikationen (soweit nicht frei verfügbar), die eigenen Manuskripte der "
        "letzten 3 Jahre und optional weitere Publikationen hochladen. Fehlende Leitlinien fordert die App "
        "gezielt an. Danach erstellt die KI alles Weitere bis zum fertigen Manuskript mit Prüfprotokoll.\n\n"
        "Keine Patientendaten hochladen: Texte werden zur Verarbeitung an die Anthropic-API gesendet. "
        "Erzeugte Dateien bitte immer sofort herunterladen."
    )


def out_any(p: dict) -> bool:
    return bool((p.get("outputs") or {}).get("manuskript_DE"))


def get_llm(budget_usd: float = 0.0) -> LLM | None:
    if not api_key:
        st.warning("Bitte zuerst den Anthropic-API-Schlüssel in der Seitenleiste eingeben.")
        return None
    return LLM(api_key, budget_usd=budget_usd)


def run_with_status(label: str, fn, llm: LLM | None = None) -> None:
    before = llm.cost_usd if llm else 0.0
    with st.status(label, expanded=True) as status:
        line = st.empty()
        started = time.time()

        def log(msg: str) -> None:
            line.write(f"{msg} ({int(time.time() - started)} Sek.)")

        try:
            fn(log)
            status.update(label=f"{label} – fertig", state="complete", expanded=False)
        except Exception as ex:
            status.update(label=f"{label} – abgebrochen", state="error")
            st.error(f"Abgebrochen: {ex}\n\nEin erneuter Start setzt an derselben Stelle fort; bereits "
                     "Erledigtes wird nicht noch einmal bezahlt. Zur Sicherheit den Zwischenstand in der "
                     "Seitenleiste herunterladen.")
        finally:
            if llm:
                spent = llm.cost_usd - before
                proj["cost_usd"] = proj.get("cost_usd", 0.0) + spent
                st.caption(f"KI-Kosten dieses Laufs ≈ {spent:.2f} USD ({llm.web_searches} Websuchen); "
                           f"Projekt bisher ≈ {proj['cost_usd']:.2f} USD (Listenpreise, Schätzung).")


# ======================================================================
# SCHRITT 1
# ======================================================================
st.markdown("<div style='font-size:2.2rem; font-weight:700;'>1. Thema und Vorgaben</div>", unsafe_allow_html=True)

with st.form("step1"):
    email = st.text_input("E-Mail-Adresse", value=proj.get("email") or secret("UNPAYWALL_EMAIL"),
                          help="Wird für PubMed und Unpaywall (Open-Access-Prüfung) als Kontaktadresse benötigt.")
    c1, c2, c3 = st.columns(3)
    with c1:
        toc_file = st.file_uploader("Inhaltsverzeichnis", type=["docx", "pdf", "txt"])
    with c2:
        template_file = st.file_uploader("Manuskript-Formatvorlage", type=["docx"])
    with c3:
        guide_file = st.file_uploader("Hinweise für Autoren", type=["docx", "pdf", "txt"])
    go1 = st.form_submit_button("🔍 Literaturempfehlung und Leitlinienrecherche starten")

if go1:
    llm = get_llm()
    if llm and not email.strip():
        st.warning("Bitte eine E-Mail-Adresse eingeben.")
    elif llm and not (toc_file or proj.get("toc")):
        st.warning("Bitte das Inhaltsverzeichnis hochladen.")
    elif llm:
        proj["email"] = email.strip()
        toc_text = extract_text(toc_file.name, toc_file.read()) if toc_file else ""
        guide_text = extract_text(guide_file.name, guide_file.read()) if guide_file else ""
        template = (template_file.name, template_file.read()) if template_file else None

        def step1(log):
            pipeline.run_intake(llm, proj, toc_text, guide_text, template, log)
            pipeline.run_recommendations(llm, proj, log)
            pipeline.run_guideline_search(llm, proj, log)

        run_with_status("Schritt 1 läuft", step1, llm)

if proj.get("toc"):
    toc = proj["toc"]
    st.subheader(f"Kapitel {toc.get('chapter_number', '')} {toc.get('chapter_title', '')}".strip())
    rules = proj.get("rules") or {}
    if rules:
        with st.expander("📋 Erkannte Vorgaben aus den Hinweisen für Autoren"):
            st.json({k: v for k, v in rules.items() if v})
    if proj.get("template"):
        st.caption(f"Formatvorlage: {proj['template']['name']} · erkannte med-update-Formatvorlagen: "
                   f"{', '.join(proj['template'].get('med_update_styles', [])) or 'keine'}")

    lp = proj.get("length_plan") or {}
    plan_tiers = lp.get("tiers", {})
    if lp:
        st.caption(f"Ausgewählt werden nur Arbeiten der Priorität A. Geschätzter Umfang ≈{lp['pages']} Seiten"
                   + (f" (max. {lp['max_pages']} laut Hinweisen für Autoren)" if lp.get("max_pages") else "")
                   + (f"; {len(lp['overflow'])} Arbeit(en) wegen der Gesamtlänge in die Reserve verschoben."
                      if lp.get("overflow") else "."))

    for tid, t in proj["topics"].items():
        rec_ids = t.get("recommended")
        head = f"{t.get('number', '')} {t['title']}".strip()
        with st.expander(f"📚 {head}" + (f" – {len(rec_ids)} Empfehlungen" if rec_ids is not None else "")):
            t["enabled"] = st.checkbox("Thema in das Manuskript aufnehmen", value=t.get("enabled", True),
                                       key=f"en_{tid}")
            if t.get("rec_error"):
                st.error(f"Die Literaturrecherche ist für dieses Thema fehlgeschlagen: {t['rec_error']}. "
                         "Ein erneuter Start von Schritt 1 wiederholt nur die fehlgeschlagenen Themen.")
            if rec_ids is None:
                if not t.get("rec_error"):
                    st.info("Noch keine Literaturempfehlung.")
                continue
            if not rec_ids:
                st.warning("Keine Arbeit der Priorität A gefunden. Bei Bedarf eine Arbeit aus der Reserve anhaken.")
            st.caption(f"Zeitraum {t['window'][0]} bis {t['window'][1]} · {t['n_candidates']} Kandidaten bewertet · "
                       f"Treffer je Suchstrategie: {t.get('query_hits')}")
            with st.popover("🔎 Verwendete PubMed-Suchen"):
                for a in t.get("attempts") or [{"window": t["window"], "queries": t.get("plan", {}).get("queries", []),
                                                "hits": t.get("query_hits", {})}]:
                    st.markdown(f"**Zeitraum {a['window'][0]} bis {a['window'][1]}**")
                    for q in a["queries"]:
                        st.code(q["query"], language=None)
                        st.caption(f"{q['name']}: {a['hits'].get(q['name'], '?')} Treffer")
            selected = list(t.get("selected", rec_ids))
            ranking = t.get("ranking", {})
            for pmid in rec_ids + t.get("reserve", []):
                rec = proj["records"].get(pmid, {})
                it = ranking.get(pmid, {})
                oa = " · 🔓 Open Access" if rec.get("is_oa") else ""
                label = (f"**{rec.get('title', pmid)}** — {rec.get('journal', '')} {rec.get('year', '')}{oa}  \n"
                         f"Priorität {it.get('priority', '?')} · Qualität {it.get('quality', '?')} · "
                         f"Bedeutung {it.get('question_importance', '?')} · "
                         f"IF {it.get('journal_impact_factor') or 'k. A.'} · geplante Länge "
                         f"{plan_tiers.get(pmid, 'Reserve')} · "
                         f"{it.get('study_design', '')} · [PubMed](https://pubmed.ncbi.nlm.nih.gov/{pmid}/)"
                         + (f" · [DOI](https://doi.org/{rec['doi']})" if rec.get("doi") else "")
                         + f"  \n_{it.get('rationale', '')}_")
                checked = st.checkbox(label, value=pmid in selected, key=f"sel_{tid}_{pmid}")
                if checked and pmid not in selected:
                    selected.append(pmid)
                if not checked and pmid in selected:
                    selected.remove(pmid)
            t["selected"] = selected
            gl = t.get("guidelines")
            if gl:
                st.markdown("**Aktuelle Leitlinien**")
                for g in gl:
                    icon = "✅" if g.get("text") else "📥"
                    link = g.get("page_url") or g.get("pdf_url")
                    st.markdown(f"{icon} {g.get('title', '')} ({g.get('society', '')}, {g.get('year', '')})"
                                + (f" · [Link]({link})" if link else ""))
            elif gl is not None:
                st.caption("Keine Leitlinie gefunden.")

    if any("recommended" in t for t in proj["topics"].values()):
        st.download_button("⬇️ Literaturempfehlung als Word-Datei", data=pipeline.recommendation_docx(proj),
                           file_name=f"Literaturempfehlung_{date.today().isoformat()}.docx", mime=DOCX_MIME)

    if proj["errors"]:
        with st.expander(f"⚠️ {len(proj['errors'])} Hinweis(e) aus der Verarbeitung"):
            for e in proj["errors"]:
                st.write(f"**{e['where']}**: {e['error']}")

# ======================================================================
# SCHRITT 2
# ======================================================================
if any("recommended" in t for t in proj["topics"].values()):
    st.markdown("<div style='font-size:2.2rem; font-weight:700; margin-top:2rem;'>2. Volltexte und eigene "
                "Manuskripte</div>", unsafe_allow_html=True)

    wanted = pipeline.selected_pmids(proj)
    n_sec = len({t.get("section_number") or tid for tid, t in proj["topics"].items() if t.get("enabled", True)})
    st.info(f"Grobe Schätzung der KI-Kosten für Schritt 2: etwa {len(wanted) * 0.6 + n_sec * 0.3 + 1:.0f}–"
            f"{len(wanted) * 1.5 + n_sec * 0.6 + 3:.0f} USD für {len(wanted)} Publikationen. Die tatsächlichen "
            "Kosten misst die App und zeigt sie nach jedem Lauf an; das Kostenlimit unten hält den Lauf "
            "rechtzeitig an.")
    missing_ft = [p for p in wanted if not proj["pubs"].get(p, {}).get("full_text")
                  and not proj["records"].get(p, {}).get("is_oa")]
    if missing_ft:
        st.write("**Bitte diese Originalpublikationen als PDF hochladen** (nicht frei verfügbar):")
        for p in missing_ft:
            r = proj["records"].get(p, {})
            st.markdown(f"- {r.get('title', p)} — {r.get('journal', '')} {r.get('year', '')}"
                        + (f" · [DOI](https://doi.org/{r['doi']})" if r.get("doi") else ""))
    st.caption("Frei verfügbare Volltexte lädt die App selbst. Hochgeladene PDFs werden automatisch der "
               "passenden Publikation zugeordnet; Dateien, die zu keiner Empfehlung passen, werden als zusätzliche "
               "Publikation gewürdigt und dem passenden Thema zugeordnet.")

    missing_gl = [(tid, g) for tid, t in proj["topics"].items() if t.get("enabled", True)
                  for g in t.get("guidelines", []) if not g.get("text")]
    if missing_gl:
        st.write("**Diese Leitlinien konnte die App nicht selbst laden – bitte, falls vorhanden, hochladen:**")
        for tid, g in missing_gl:
            st.markdown(f"- {g.get('title', '')} ({g.get('society', '')}, {g.get('year', '')}) — Thema: "
                        f"{proj['topics'][tid]['title']}")

    with st.form("step2"):
        pub_files = st.file_uploader("Originalpublikationen und weitere Publikationen (PDF)", type=["pdf"],
                                     accept_multiple_files=True)
        style_files = st.file_uploader("Eigene Manuskripte der letzten 3 Jahre (für den Schreibstil)",
                                       type=["docx", "pdf"], accept_multiple_files=True)
        gl_files = st.file_uploader("Leitlinien (PDF)", type=["pdf"], accept_multiple_files=True)
        o1, o2, o3 = st.columns(3)
        opt_fig = o1.checkbox("Abbildungen automatisch auswählen", value=True)
        opt_sota = o2.checkbox("State of the Art (einer je Hauptabschnitt)", value=True)
        opt_style = o3.checkbox("An meinen Schreibstil anpassen", value=True)
        opt_joint = st.checkbox("Gemeinsame Würdigung für Publikationen mit nahezu gleicher Fragestellung (2–3 je Gruppe)",
                                value=True, help="Wie Schritt 4B in MyLuo. Die Einzelwürdigungen werden zu einer "
                                                 "vergleichenden Würdigung zusammengeführt.")
        opt_own = st.checkbox("Inhaltliche Rückbezüge auf meine früheren Manuskripte (höchstens 2, nur wo sinnvoll)",
                              value=True, help="Nur wenn eigene Manuskripte hochgeladen sind. Ein zusätzlicher KI-Aufruf.")
        b1, b2 = st.columns(2)
        opt_trial = b1.checkbox("Probelauf: nur erstes Thema, höchstens 2 Publikationen (empfohlen vor dem ersten "
                                "vollständigen Lauf)", value=not out_any(proj))
        trial_topics = list(dict.fromkeys(t for _, t in pipeline._pub_list(proj)))
        opt_trial_topic = b1.selectbox(
            "Thema für den Probelauf", trial_topics,
            format_func=lambda t: proj["topics"][t].get("number", "") + " " + proj["topics"][t]["title"],
        ) if trial_topics else None
        opt_budget = b2.number_input("Kostenlimit für diesen Lauf (USD)", min_value=1, max_value=500, value=15,
                                     help="Ist das Limit erreicht, hält die App an. Alles Fertige bleibt erhalten; "
                                          "ein erneuter Start setzt dort fort.")
        n_old = sum(1 for d in proj.get("pubs", {}).values()
                    if d.get("short_en") and d.get("short_v", 1) < pipeline.SHORT_VERSION)
        opt_reshorten = st.checkbox(
            f"Bereits gekürzte Würdigungen neu kürzen (kürzere Sätze, weniger Statistik; {n_old} betroffen, "
            f"ca. {0.2 * n_old:.0f} USD)", value=False, disabled=not n_old,
            help="Die kritischen Würdigungen selbst werden nicht neu erstellt, nur Kürzung, Übersetzung und Stil.")
        go2 = st.form_submit_button("✍️ Manuskript automatisch erstellen")

    if go2:
        llm = get_llm(float(opt_budget))
        if llm:
            uploads = {f.name: f.read() for f in (pub_files or [])}
            for f in style_files or []:
                proj["style"][f.name] = extract_text(f.name, f.read())[:20000]
            topics_for_gl = {g.get("title", "").lower(): tid for tid, g in missing_gl}
            for f in gl_files or []:
                text = extract_text(f.name, f.read())
                tid = next((t for title, t in topics_for_gl.items() if title and title[:30] in text.lower()[:5000]), "")
                proj["guideline_uploads"][f.name] = {"topic_id": tid, "text": text}

            def step2(log):
                if uploads:
                    pipeline.match_uploads(llm, proj, uploads, log)
                pipeline.run_manuscript(llm, proj, log, {"figures": opt_fig, "sota": opt_sota, "style": opt_style,
                                                         "trial": opt_trial, "trial_topic": opt_trial_topic, "own_refs": opt_own, "joint": opt_joint,
                                                         "reshorten": opt_reshorten})

            run_with_status("Manuskript wird erstellt", step2, llm)

    out = proj.get("outputs") or {}
    partial = pipeline.partial_docx(proj) if any(d.get("body_en") for d in proj["pubs"].values()) else None
    if partial:
        st.download_button("⬇️ Bisher fertige Würdigungen herunterladen (auch nach Abbruch)", partial,
                           f"Bisherige_Ergebnisse_{date.today().isoformat()}.docx", DOCX_MIME)
    if out.get("manuskript_DE"):
        st.subheader("Ergebnis")
        d1, d2, d3 = st.columns(3)
        today = date.today().isoformat()
        d1.download_button("⬇️ Manuskript (Deutsch)", out["manuskript_DE"], f"Manuskript_DE_{today}.docx", DOCX_MIME)
        d2.download_button("⬇️ Manuskript (Englisch)", out["manuskript_EN"], f"Manuskript_EN_{today}.docx", DOCX_MIME)
        d3.download_button("⬇️ Prüfprotokoll", out["pruefprotokoll"], f"Pruefprotokoll_{today}.docx", DOCX_MIME)
        with st.expander("Prüfprotokoll ansehen"):
            for b in out.get("befunde", []):
                st.write(f"{b['status']} **{b['punkt']}**")
                st.caption(b["begruendung"])
        with st.expander("Längenstufen je Publikation"):
            for p, tier in (proj.get("tiers") or {}).items():
                st.write(f"{tier}: {proj['records'].get(p, {}).get('title', p)}")
