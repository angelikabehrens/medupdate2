"""MyLuo Pro – nahezu automatisierte Erstellung eines wissenschaftlichen Fortbildungsmanuskripts.

Zwei Schritte für die Nutzerin bzw. den Nutzer:
1. Inhaltsverzeichnis, Formatvorlage und Hinweise für Autoren hochladen → KI-gestützte
   Literaturempfehlung und Leitlinienrecherche.
2. Volltexte, eigene frühere Manuskripte und ggf. fehlende Leitlinien hochladen → die KI erstellt
   kritische Würdigungen, Kürzungen, State of the Art, Übersetzung, Stilanpassung,
   Literaturverzeichnis, Formatierung und formale Prüfung.
"""

import calendar
import hmac
import threading
import time
import uuid
from datetime import date

import streamlit as st

from medupdate import pipeline
from medupdate.config import APP_VERSION, secret
from medupdate.files import extract_text
from medupdate.llm import LLM

# Kostenlimit für den Pflichtschritt „Hochladen und Themenzuordnung prüfen“ (wenige KI-Aufrufe)
CHECK_BUDGET_USD = 5.0

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

@st.cache_resource
def _jobs() -> dict:
    """Hintergrund-Läufe aller Sitzungen dieser App ({Projekt-ID: Lauf}); überlebt einen Seiten-Neuladen."""
    return {}


if "proj" not in st.session_state:
    st.session_state["proj"] = pipeline.new_project()
proj = st.session_state["proj"]
proj.setdefault("id", uuid.uuid4().hex[:12])
_running = _jobs().get(proj["id"], {}).get("state") == "running"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# ---------- Seitenleiste ----------
_SHARED_KEY = secret("ANTHROPIC_API_KEY")
with st.sidebar:
    st.caption(f"Programmstand {APP_VERSION}")
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
    _others = {pid: j for pid, j in _jobs().items() if pid != proj["id"] and not j.get("taken")}
    for _pid, _j in _others.items():
        _state = {"running": "läuft", "done": "fertig", "error": "abgebrochen"}[_j["state"]]
        if st.button(f"▶️ Lauf übernehmen: {_j['label']} ({_state})", key=f"take_{_pid}"):
            _j["taken"] = True
            st.session_state["proj"] = _j["proj"]
            st.rerun()
    if _running:
        st.caption("Während eines Laufs kann der Zwischenstand nicht gespeichert werden.")
    elif st.button("💾 Zwischenstand vorbereiten"):
        st.session_state["_export"] = pipeline.export_project(proj)
    if st.session_state.get("_export"):
        st.download_button("⬇️ Zwischenstand herunterladen", data=st.session_state["_export"],
                           file_name=f"medupdate2_zwischenstand_{date.today().isoformat()}.json.gz",
                           mime="application/gzip", on_click=lambda: st.session_state.pop("_export", None))
    _load = st.file_uploader("Zwischenstand laden", type=["gz", "json"], key="load_state")
    if _load is not None and not _running and st.button("Laden"):
        st.session_state["proj"] = pipeline.import_project(_load.read())
        st.rerun()
    if not _running and st.button("🗑️ Neues Projekt beginnen"):
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
    """Startet den Lauf im Hintergrund. Er läuft auf dem Server weiter, auch wenn das Browserfenster
    geschlossen wird, der Rechner in den Ruhezustand geht oder die Verbindung kurz abreißt; die Seite zeigt
    nur den Fortschritt an. (Ein Neustart der App selbst beendet ihn; Fertiges bleibt im Zwischenstand.)"""
    job = {"label": label, "proj": proj, "log": [], "state": "running", "error": "", "started": time.time(),
           "llm": llm, "before": llm.cost_usd if llm else 0.0, "spent": 0.0}

    def log(msg: str) -> None:
        job["log"] = (job["log"] + [f"{msg} ({int(time.time() - job['started'])} Sek.)"])[-8:]

    def target() -> None:
        try:
            fn(log)
            job["state"] = "done"
        except Exception as ex:  # noqa: BLE001 – jede Ursache wird angezeigt und im Zwischenstand vermerkt
            job["state"], job["error"] = "error", f"{type(ex).__name__}: {ex}"
            proj.setdefault("errors", []).append({"where": f"Abbruch: {label}", "error": job["error"]})
        finally:
            if llm:
                job["spent"] = llm.cost_usd - job["before"]
                proj["cost_usd"] = proj.get("cost_usd", 0.0) + job["spent"]
            job["ended"] = time.time()

    _jobs()[proj["id"]] = job
    threading.Thread(target=target, daemon=True, name=f"lauf-{proj['id']}").start()
    st.rerun()


def show_job() -> None:
    """Laufender Hintergrund-Lauf: nur den Fortschritt zeigen (alle 5 Sek. aktualisiert), sonst nichts
    bedienen, damit Eingaben den Lauf nicht stören. Ein beendeter Lauf zeigt einmal sein Ergebnis."""
    job = _jobs().get(proj["id"])
    if not job:
        return
    if job["state"] == "running":
        @st.fragment(run_every=5)
        def progress():
            if job["state"] != "running":
                st.rerun()
            llm = job.get("llm")
            spent = (llm.cost_usd - job["before"]) if llm else 0.0
            with st.status(f"{job['label']} … läuft seit {int((time.time() - job['started']) / 60)} Min.",
                           expanded=True):
                for line in job["log"][-5:]:
                    st.write(line)
                st.caption(f"KI-Kosten dieses Laufs bisher ≈ {spent:.2f} USD. Sie können das Fenster schließen; "
                           "der Lauf geht auf dem Server weiter. Später diese Seite öffnen und den Lauf in der "
                           "Seitenleiste übernehmen.")
            if llm and st.button("⏹ Lauf anhalten (Fertiges bleibt erhalten)"):
                llm.stop_requested = True  # der nächste KI-Aufruf hält sauber an
                st.toast("Wird nach dem laufenden Schritt angehalten.")
        progress()
        st.stop()
    if not job.get("shown"):
        job["shown"] = True
        if job["state"] == "done":
            st.success(f"{job['label']} – fertig.")
        else:
            st.error(f"{job['label']} – abgebrochen: {job['error']}\n\nEin erneuter Start setzt an derselben "
                     "Stelle fort; bereits Erledigtes wird nicht noch einmal bezahlt.")
        if job.get("llm"):
            st.caption(f"KI-Kosten dieses Laufs ≈ {job['spent']:.2f} USD ({job['llm'].web_searches} Websuchen); "
                       f"Projekt bisher ≈ {proj.get('cost_usd', 0):.2f} USD (Listenpreise, Schätzung).")


# ======================================================================
# SCHRITT 1
# ======================================================================
show_job()
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
    _lim = proj.get("manual_limits") or {}
    l1, l2, l3 = st.columns(3)
    lim_pages = l1.number_input("Anzahl Manuskriptseiten (ohne Inhaltsverzeichnis)", min_value=0, max_value=500,
                                value=int(_lim.get("pages") or 0), step=1,
                                help=f"0 = Angabe aus den Hinweisen für Autoren. Überschreitung um höchstens "
                                     f"{pipeline.tolerance_text(proj)}.")
    lim_refs = l2.number_input("Anzahl der Literaturstellen", min_value=0, max_value=1000,
                               value=int(_lim.get("references") or 0), step=1,
                               help=f"0 = Angabe aus den Hinweisen für Autoren. Überschreitung um höchstens "
                                     f"{pipeline.tolerance_text(proj)}.")
    lim_pubs = l3.number_input("Höchstzahl gewürdigter Publikationen", min_value=0, max_value=500,
                               value=int(_lim.get("publications") or 0), step=1,
                               help="0 = keine feste Zahl (es gilt nur der Seitenumfang). Wird nicht überschritten: "
                                    "nach Hochladen und Sortieren entfernt die App bei Bedarf die Studien mit der "
                                    "geringsten Qualität.")
    b_win, b_go = st.columns([1, 3])
    with b_win.popover("📅 Suchzeitraum"):
        _months = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]
        _years = list(range(date.today().year, date.today().year - 16, -1))
        _cur = proj.get("search_window")
        _von = date.fromisoformat(_cur[0]) if _cur else date(date.today().year - 1, date.today().month, 1)
        _bis = date.fromisoformat(_cur[1]) if _cur else date.today()
        win_manual = st.checkbox("Zeitraum manuell festlegen", value=bool(_cur),
                                 help="Ohne Häkchen gilt der Zeitraum aus den Hinweisen für Autoren (sonst 12 Monate).")
        st.caption("von (Monat-Jahr)")
        v1, v2 = st.columns(2)
        von_m = v1.selectbox("Monat von", _months, index=_von.month - 1, label_visibility="collapsed")
        von_y = v2.selectbox("Jahr von", _years, index=_years.index(_von.year) if _von.year in _years else 0,
                             label_visibility="collapsed")
        st.caption("bis (Monat-Jahr)")
        w1, w2 = st.columns(2)
        bis_m = w1.selectbox("Monat bis", _months, index=_bis.month - 1, label_visibility="collapsed")
        bis_y = w2.selectbox("Jahr bis", _years, index=_years.index(_bis.year) if _bis.year in _years else 0,
                             label_visibility="collapsed")
    go1 = b_go.form_submit_button("🔍 Literaturempfehlung und Leitlinienrecherche starten")

if proj.get("search_window"):
    _w = [date.fromisoformat(x) for x in proj["search_window"]]
    st.caption(f"📅 Suchzeitraum manuell: {_w[0].strftime('%m-%Y')} bis {_w[1].strftime('%m-%Y')}")

if go1:
    llm = get_llm()
    if llm and not email.strip():
        st.warning("Bitte eine E-Mail-Adresse eingeben.")
    elif llm and not (toc_file or proj.get("toc")):
        st.warning("Bitte das Inhaltsverzeichnis hochladen.")
    elif llm:
        proj["email"] = email.strip()
        proj["manual_limits"] = {"pages": int(lim_pages), "references": int(lim_refs), "publications": int(lim_pubs)}
        if win_manual:
            _last = calendar.monthrange(int(bis_y), int(bis_m))[1]
            proj["search_window"] = [date(int(von_y), int(von_m), 1).isoformat(),
                                     date(int(bis_y), int(bis_m), _last).isoformat()]
        else:
            proj.pop("search_window", None)
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
            st.json({k: v for k, v in rules.items() if v and not k.startswith("_")})
    _ml = proj.get("manual_limits") or {}
    if _ml.get("pages") or _ml.get("references") or _ml.get("publications"):
        st.caption(f"Manuelle Vorgaben (Überschreitung um höchstens {pipeline.tolerance_text(proj)}): "
                   f"Obergrenze {rules.get('max_pages', 0) - 1 if rules.get('max_pages') else '–'} Seiten ohne "
                   f"Inhaltsverzeichnis, {rules.get('max_references') or '–'} Literaturstellen"
                   + (f", höchstens {_ml['publications']} gewürdigte Publikationen." if _ml.get("publications") else "."))
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
        if t.get("varia"):
            extras = [p for p, d in proj["pubs"].items() if d.get("extra") and d.get("topic_id") == tid]
            with st.expander(f"📚 Varia – {len(extras)} hochgeladene Publikation(en) ohne passendes Thema"):
                t["enabled"] = st.checkbox("Varia in das Manuskript aufnehmen", value=t.get("enabled", True),
                                           key=f"en_{tid}")
                for p in extras:
                    st.markdown(f"- {proj['records'].get(p, {}).get('title', p)}")
            continue
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
            moved_to = pipeline.current_topics(proj)
            limit_dropped = {e["id"]: e["reason"] for e in proj.get("limit_dropped", [])}
            for pmid in dict.fromkeys(rec_ids + t.get("reserve", [])):
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
                if pmid in limit_dropped:
                    label += f"  \n✂️ wegen der Vorgaben entfernt ({limit_dropped[pmid]})"
                elsewhere = moved_to.get(pmid)
                if elsewhere and elsewhere != tid:
                    label += f"  \n↪ verschoben nach „{proj['topics'][elsewhere]['title']}“"
                checked = st.checkbox(label, value=pmid in selected, key=f"sel_{tid}_{pmid}_{proj.get('sel_rev', 0)}",
                                      disabled=bool(elsewhere and elsewhere != tid))
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

    wanted = list(dict.fromkeys(pipeline.selected_pmids(proj)))
    n_sec = len({t.get("section_number") or tid for tid, t in proj["topics"].items() if t.get("enabled", True)})
    st.info(f"Grobe Schätzung der KI-Kosten für Schritt 2: etwa {len(wanted) * 0.6 + n_sec * 0.3 + 1:.0f}–"
            f"{len(wanted) * 1.5 + n_sec * 0.6 + 3:.0f} USD für {len(wanted)} Publikationen. Die tatsächlichen "
            "Kosten misst die App und zeigt sie nach jedem Lauf an; das Kostenlimit unten hält den Lauf "
            "rechtzeitig an.")
    need_pdf = [p for p in wanted if proj["pubs"].get(p, {}).get("fulltext_source") == "Upload"
                or (not proj["pubs"].get(p, {}).get("full_text") and not proj["records"].get(p, {}).get("is_oa"))]
    if need_pdf:
        st.write("**Bitte diese Originalpublikationen als PDF hochladen** (nicht frei verfügbar) – direkt bei der "
                 "jeweiligen Publikation. Die App prüft sofort, ob die Datei zur Publikation passt.")
        for p in need_pdf:
            r, d = proj["records"].get(p, {}), proj["pubs"].get(p, {})
            c_info, c_up = st.columns([3, 2])
            c_info.markdown(f"**{r.get('title', p)}**  \n{(r.get('authors') or [''])[0]} · {r.get('journal', '')} "
                            f"{r.get('year', '')}" + (f" · [DOI](https://doi.org/{r['doi']})" if r.get("doi") else ""))
            if d.get("upload_name"):
                c_info.success(f"✅ {d['upload_name']} – {d.get('upload_checked', 'passt')}")
            f = c_up.file_uploader("PDF zu dieser Publikation", type=["pdf"], key=f"up_{p}",
                                   label_visibility="collapsed")
            if f is not None and f.name != d.get("upload_name"):
                data = f.getvalue()
                res = pipeline.attach_direct_upload(proj, p, f.name, data)
                if res["ok"]:
                    st.rerun()
                if res["empty"]:
                    c_up.warning("⚠️ Aus der PDF ließ sich kein Text lesen (eingescannt?). Bitte eine andere Fassung "
                                 "hochladen.")
                else:
                    c_up.warning("⚠️ Die Datei scheint nicht zu dieser Publikation zu passen (Titel/DOI/Erstautor "
                                 f"nicht gefunden). Gefunden: „{res['found']}“")
                    if c_up.button("Trotzdem verwenden", key=f"force_{p}"):
                        pipeline.attach_direct_upload(proj, p, f.name, data, force=True)
                        st.rerun()
    st.caption("Frei verfügbare Volltexte lädt die App selbst. Im Feld unten können Sie weitere Arbeiten "
               "hochladen; sie werden automatisch der passenden Publikation oder als zusätzliche Publikation dem "
               "passenden Thema zugeordnet.")

    missing_gl = [(tid, g) for tid, t in proj["topics"].items() if t.get("enabled", True)
                  for g in t.get("guidelines", []) if not g.get("text")]
    if missing_gl:
        st.write("**Diese Leitlinien konnte die App nicht selbst laden – bitte, falls vorhanden, hochladen:**")
        for tid, g in missing_gl:
            st.markdown(f"- {g.get('title', '')} ({g.get('society', '')}, {g.get('year', '')}) — Thema: "
                        f"{proj['topics'][tid]['title']}")

    # 2a. Hochladen und Themenzuordnung prüfen – immer vor dem Schreiben
    with st.form("step2_upload"):
        pub_files = st.file_uploader("Weitere Publikationen (PDF)", type=["pdf"],
                                     accept_multiple_files=True)
        style_files = st.file_uploader("Eigene Manuskripte der letzten 3 Jahre (für den Schreibstil)",
                                       type=["docx", "pdf"], accept_multiple_files=True)
        gl_files = st.file_uploader("Leitlinien (PDF)", type=["pdf"], accept_multiple_files=True)
        st.caption("Danach prüft die KI die Themenzuordnung aller Publikationen (Schwerpunkt der Arbeit; gleiche "
                   "Fragestellung → gleiches Thema, gemeinsame Würdigung) und hält Publikationszahl und Seitenumfang "
                   "ein. Erst dann kann das Manuskript erstellt werden. Kosten ca. 0,2–1 USD.")
        go_check = st.form_submit_button("🧩 Hochladen und Themenzuordnung prüfen", type="primary")

    if go_check:
        llm = get_llm(CHECK_BUDGET_USD)
        if llm:
            uploads = {f.name: f.read() for f in (pub_files or [])}
            for f in style_files or []:
                proj["style"][f.name] = extract_text(f.name, f.read())[:20000]
            topics_for_gl = {g.get("title", "").lower(): tid for tid, g in missing_gl}
            for f in gl_files or []:
                text = extract_text(f.name, f.read())
                tid = next((t for title, t in topics_for_gl.items() if title and title[:30] in text.lower()[:5000]), "")
                proj["guideline_uploads"][f.name] = {"topic_id": tid, "text": text}
            run_with_status("Themenzuordnung wird geprüft",
                            lambda log: pipeline.check_assignment(llm, proj, uploads, log), llm)

    if proj.get("reassigned_for"):
        overview = pipeline.assignment_overview(proj)
        n_moved = sum(1 for r in overview if r["kind"] == "topic" for x in r["pubs"] if x["moved_from"])
        n_pubs = sum(len(r["pubs"]) for r in overview if r["kind"] == "topic")
        with st.expander(f"📋 Themenzuordnung: {n_pubs} Publikationen ({n_moved} verschoben, "
                         f"{len(proj.get('limit_dropped', []))} wegen der Vorgaben entfernt)", expanded=go_check):
            st.caption("Überschriften des Inhaltsverzeichnisses mit den zugeordneten Publikationen. ↪ = von der KI "
                       "(oder von Ihnen) in ein anderes Thema verschoben, 🔗 = gemeinsame Würdigung. Ändern Sie eine "
                       "Zuordnung bei Bedarf unten; die KI übernimmt Ihre Festlegung.")
            for r in overview:
                if r["kind"] == "section":
                    st.markdown(f"**{r['text']}**")
                    continue
                st.markdown(f"&nbsp;&nbsp;&nbsp;*{r['text']}*" + ("" if r["pubs"] else " — keine Publikation"))
                for x in r["pubs"]:
                    line = f"- {x['title']} — {x['journal']} {x['year']}" + (" · hochgeladen" if x["uploaded"] else "")
                    if x["moved_from"]:
                        line += f"  \n  ↪ aus „{x['moved_from']}“" + (f": {x['reason']}" if x["reason"] else "")
                    if x["joint"]:
                        line += f"  \n  🔗 Gemeinsame Würdigung: {x['joint']}"
                    st.markdown(line)
            if proj.get("limit_dropped"):
                st.markdown(f"**✂️ Wegen der Vorgaben nicht aufgenommen ({len(proj['limit_dropped'])}, geringste "
                            "Studienqualität zuerst):**")
                for e in proj["limit_dropped"]:
                    st.markdown(f"- {e['title']} — {proj['topics'].get(e['topic_id'], {}).get('title', '')} · {e['reason']}")
            st.download_button("⬇️ Themenzuordnung als Word-Datei", pipeline.assignment_docx(proj),
                               f"Themenzuordnung_{date.today().isoformat()}.docx", DOCX_MIME)
            cur = pipeline.current_topics(proj)
            tids = [tid for tid, t in proj["topics"].items() if t.get("enabled", True)]
            with st.form("reassign_manual"):
                pick = st.selectbox("Publikation", list(cur),
                                    format_func=lambda p: proj["records"].get(p, {}).get("title", p)[:120])
                dst = st.selectbox("Neues Thema", tids, format_func=lambda t: (proj["topics"][t].get("number", "")
                                                                             + " " + proj["topics"][t]["title"]).strip())
                if st.form_submit_button("Zuordnung ändern") and pick:
                    if dst == pipeline.VARIA and not proj["pubs"].get(pick, {}).get("extra"):
                        st.warning("Unter „Varia“ stehen nur zusätzlich hochgeladene Publikationen.")
                    else:
                        pipeline.set_manual_topic(proj, pick, dst)
                        st.rerun()

    # 2b. Manuskript erstellen – erst nach der Prüfung der Themenzuordnung
    ready = bool(proj.get("check_ok"))
    if not ready:
        st.info("Bitte zuerst „🧩 Hochladen und Themenzuordnung prüfen“ ausführen (auch wenn keine PDFs "
                "hochzuladen sind). Danach wird „Manuskript erstellen“ freigegeben.")
    with st.form("step2_write"):
        o2, o3 = st.columns(2)
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
            f"Bereits gekürzte Würdigungen neu kürzen (verständlichere Methodik/Statistik, abwechslungsreiches "
            f"Fazit, kürzere Sätze; {n_old} betroffen, ca. {0.3 * n_old:.0f} USD)", value=False, disabled=not n_old,
            help="Die kritischen Würdigungen selbst werden nicht neu erstellt, nur Kürzung, Übersetzung und Stil.")
        go2 = st.form_submit_button("✍️ Manuskript automatisch erstellen", disabled=not ready, type="primary")

    if go2 and ready:
        llm = get_llm(float(opt_budget))
        if llm:
            run_with_status("Manuskript wird erstellt", lambda log: pipeline.run_manuscript(llm, proj, log, {
                "sota": opt_sota, "style": opt_style, "trial": opt_trial,
                "trial_topic": opt_trial_topic, "own_refs": opt_own, "joint": opt_joint,
                "reshorten": opt_reshorten}), llm)

    out = proj.get("outputs") or {}
    partial = pipeline.partial_docx(proj) if any(d.get("body_en") for d in proj["pubs"].values()) else None
    if partial:
        st.download_button("⬇️ Bisher fertige Würdigungen herunterladen (auch nach Abbruch)", partial,
                           f"Bisherige_Ergebnisse_{date.today().isoformat()}.docx", DOCX_MIME)
    if out.get("manuskript_DE"):
        st.subheader("Ergebnis")
        if out.get("created"):
            st.caption(f"Erstellt am {out['created'][:10]} um {out['created'][11:16]} Uhr (UTC), Programmstand "
                       f"{out.get('version', 'unbekannt')}.")
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
