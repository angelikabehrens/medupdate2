"""MyLuo Pro – nahezu automatisierte Erstellung eines wissenschaftlichen Fortbildungsmanuskripts.

Zwei Schritte für die Nutzerin bzw. den Nutzer:
1. Inhaltsverzeichnis, Formatvorlage und Hinweise für Autoren hochladen → KI-gestützte
   Literaturempfehlung und Leitlinienrecherche.
2. Volltexte, eigene frühere Manuskripte und ggf. fehlende Leitlinien hochladen → die KI erstellt
   kritische Würdigungen, Kürzungen, Überleitungen, Übersetzung, Stilanpassung,
   Literaturverzeichnis, Formatierung und formale Prüfung.
"""

import calendar
import hmac
import threading
import time
import uuid
from datetime import date

import streamlit as st

from medupdate import appraisal as A, pipeline, prior, recommend, selection_sheet, toc_merge
from medupdate.config import APP_VERSION, secret
from medupdate.files import extract_text
from medupdate.llm import LLM

# Kostenlimit für den Pflichtschritt „Hochladen und Themenzuordnung prüfen“ (wenige KI-Aufrufe)
CHECK_BUDGET_USD = 5.0
# Kostenlimit für den Vergleich „schlanke Würdigung“ (drei vollständige und drei schlanke Würdigungen)
LEAN_TEST_BUDGET_USD = 12.0
# Sparmodus in Schritt 1: Bewertung der Suchtreffer und Leitliniensuche zum halben Preis, der Rest unverändert –
# zusammen etwa ein Drittel weniger (Schätzung nach der Kostenprüfung vom 13.11.2026, noch nicht gemessen)
STEP1_BATCH_FACTOR = 0.67


def usd(value: float) -> str:
    """Betrag mit zwei Nachkommastellen und Dezimalkomma."""
    return f"{value:.2f}".replace(".", ",")

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
        st.caption(f"KI-Kosten dieses Projekts bisher ≈ {usd(proj['cost_usd'])} USD")
        _co = pipeline.cost_overview(proj)
        if _co["rows"]:
            with st.expander("📊 Kosten je Arbeitsschritt"):
                for _r in _co["rows"]:
                    st.caption(f"**{_r['step']}**: {usd(_r['usd'])} USD · {_r['calls']} "
                               + ("Aufruf" if _r["calls"] == 1 else "Aufrufe")
                               + (f", davon {_r['batch']} im Sammelauftrag" if _r["batch"] else "")
                               + (f" · {_r['searches']} Websuche{'' if _r['searches'] == 1 else 'n'}"
                                  if _r["searches"] else ""))
                if _co["earlier"] >= 0.01:
                    st.caption(f"**Vor Einführung dieser Aufstellung**: {usd(_co['earlier'])} USD")
                if _co["saved"] >= 0.01:
                    st.caption(f"Ersparnis durch den Sparmodus bisher: {usd(_co['saved'])} USD")
                if _co["runs"]:
                    st.caption("Letzte Läufe: " + " · ".join(
                        f"{_x['label']} {usd(_x['usd'])} USD ({_x['when'][8:10]}.{_x['when'][5:7]}.)"
                        for _x in reversed(_co["runs"])))
                st.caption("Listenpreise von Anthropic, von der App mitgezählt; maßgeblich ist die Abrechnung "
                           "in der Anthropic-Konsole.")
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
    if llm:
        llm.bind(proj)  # jeder KI-Aufruf zählt sofort im Projekt mit (Summe und Aufstellung je Arbeitsschritt)

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
                pipeline.note_run(proj, label, llm)
            job["ended"] = time.time()

    _jobs()[proj["id"]] = job
    threading.Thread(target=target, daemon=True, name=f"lauf-{proj['id']}").start()
    st.rerun()


def run_steps(llm: LLM | None, top: int = 4) -> str:
    """Die teuersten Arbeitsschritte eines Laufs als eine Zeile."""
    steps = sorted((llm.run_summary() if llm else {}).items(), key=lambda kv: -kv[1]["usd"])[:top]
    return " · ".join(f"{step} {usd(r['usd'])} USD" for step, r in steps if r["usd"] >= 0.005)


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
                st.caption(f"KI-Kosten dieses Laufs bisher ≈ {usd(spent)} USD"
                           + (f" ({run_steps(llm)})" if run_steps(llm) else "")
                           + ". Sie können das Fenster schließen; der Lauf geht auf dem Server weiter. Später diese "
                           "Seite öffnen und den Lauf in der Seitenleiste übernehmen.")
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
            st.caption(f"KI-Kosten dieses Laufs ≈ {usd(job['spent'])} USD ({job['llm'].web_searches} Websuchen); "
                       f"Projekt bisher ≈ {usd(proj.get('cost_usd', 0))} USD (Listenpreise, Schätzung)."
                       + (f" Davon: {run_steps(job['llm'], 6)}." if run_steps(job["llm"]) else "")
                       + " Die Aufstellung je Arbeitsschritt steht in der Seitenleiste.")


# ======================================================================
# SCHRITT 1
# ======================================================================
show_job()
# ----- Optionaler Schritt 0: Inhaltsverzeichnisse früherer Jahrgänge zusammenführen (2026-11-08) -----
_tm = proj.get("toc_merge") or {}
with st.expander("0. Inhaltsverzeichnisse früherer Jahrgänge zusammenführen (optional)",
                 expanded=bool(_tm.get("chapters")) and not proj.get("toc")):
    st.caption("Laden Sie zwei oder mehr Jahrgänge des Handbuchs hoch (PDF, EPUB, Word oder Text). Die App führt die "
               "Inhaltsverzeichnisse je Thema zusammen: Dopplungen werden gestrichen, Themen, die nur ein- oder "
               "zweimal vorkommen, aufgenommen. Hot Topics und Kapitel aus nur einem Jahrgang werden unverändert "
               "übernommen. Ergebnis: je Thema eine Word-Datei, die Sie direkt in Schritt 1 übernehmen können.")
    with st.form("step0_merge"):
        merge_files = st.file_uploader("Handbücher früherer Jahrgänge", type=["pdf", "epub", "docx", "txt"],
                                       accept_multiple_files=True)
        merge_name = st.text_input("Name des Handbuchs für die Dateinamen (optional)", value=_tm.get("handbook", ""),
                                   placeholder="z.B. Angio Update – leer lassen, dann erkennt die App den Namen")
        _lo, _hi = toc_merge.estimate_usd(3)
        go0 = st.form_submit_button("🧩 Inhaltsverzeichnisse zusammenführen (bei drei Jahrgängen ca. "
                                    f"{f'{_lo:.1f}'.replace('.', ',')}–{f'{_hi:.1f}'.replace('.', ',')} USD)")
    if go0:
        llm = get_llm(max(CHECK_BUDGET_USD, 2 * toc_merge.estimate_usd(len(merge_files or []))[1]))
        if llm and len(merge_files or []) < 2:
            st.warning("Bitte mindestens zwei Jahrgänge hochladen.")
        elif llm:
            _files = [(f.name, f.read()) for f in merge_files]
            _name = merge_name
            run_with_status("Inhaltsverzeichnisse werden zusammengeführt",
                            lambda log: toc_merge.run_toc_merge(llm, proj, _files, _name, log), llm)
    if _tm.get("chapters"):
        _hb = _tm.get("handbook", "")
        st.markdown(f"**{len(_tm['chapters'])} Themen** aus {', '.join(_tm.get('files', []))}")
        st.download_button("⬇️ Alle Inhaltsverzeichnisse als ZIP", toc_merge.zip_all(_tm),
                           file_name=f"Inhaltsverzeichnisse {_hb}.zip".replace("/", "-"), mime="application/zip")
        _labels = [f"{c['number']} {c['title']} ({', '.join(dict.fromkeys(c['years']))}; "
                   f"{', '.join(c['authors']) or 'Autor unbekannt'}; {c['how']})" for c in _tm["chapters"]]
        _i = st.selectbox("Thema ansehen", range(len(_labels)), format_func=lambda i: _labels[i], key="tm_pick")
        _ch = _tm["chapters"][_i]
        st.code(toc_merge.as_text(_ch), language=None)
        _d1, _d2 = st.columns(2)
        _d1.download_button("⬇️ Als Word-Datei", toc_merge.chapter_docx(_ch), file_name=toc_merge.filename(_ch, _hb),
                            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            key=f"tm_dl_{_i}")
        if _d2.button("➡️ Als Inhaltsverzeichnis in Schritt 1 übernehmen", key=f"tm_use_{_i}",
                      disabled=_running or bool(proj.get("toc"))):
            proj["toc_from_merge"] = {"name": toc_merge.filename(_ch, _hb), "text": toc_merge.as_text(_ch)}
            st.rerun()
        if proj.get("toc"):
            st.caption("Schritt 1 hat bereits ein Inhaltsverzeichnis. Für ein anderes Thema bitte ein neues Projekt "
                       "beginnen.")

st.markdown("<div style='font-size:2.2rem; font-weight:700;'>1. Thema und Vorgaben</div>", unsafe_allow_html=True)
if proj.get("toc_from_merge") and not proj.get("toc"):
    st.info(f"Inhaltsverzeichnis aus Schritt 0: {proj['toc_from_merge']['name']}. Es wird verwendet, wenn Sie unten "
            "kein anderes hochladen.")

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
    prior_files = st.file_uploader(
        "Vorjahresmanuskript (freiwillig)", type=["docx", "pdf", "txt"], accept_multiple_files=True,
        help="Arbeiten, die im Vorjahresmanuskript schon gewürdigt oder zitiert wurden, werden nicht erneut "
             "empfohlen (Abgleich über DOI, PMID und Titel, ohne KI-Kosten). Ohne Upload läuft die Suche wie "
             "bisher, z. B. bei einem neuen Gesamtthema. Mehrere Dateien sind möglich.")
    _lim = proj.get("manual_limits") or {}
    l1, l2, l3 = st.columns(3)
    lim_pages = l1.number_input("Anzahl Manuskriptseiten (ohne Inhaltsverzeichnis)", min_value=0, max_value=500,
                                value=int(_lim.get("pages") or 0), step=1,
                                help=f"0 = Angabe aus den Hinweisen für Autoren. {pipeline.tolerance_note(proj)}.")
    lim_refs = l2.number_input("Anzahl der Literaturstellen", min_value=0, max_value=1000,
                               value=int(_lim.get("references") or 0), step=1,
                               help=f"0 = Angabe aus den Hinweisen für Autoren. {pipeline.tolerance_note(proj)}.")
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
    review_plan = b_go.checkbox("Suchbegriffe vor der Bewertung prüfen (empfohlen)", value=proj.get("review_plan", True),
                                help="Die App erstellt zuerst je Thema die englischen PubMed-Suchen und zählt die Treffer "
                                     "(wenige Cent je Thema). Sie können die Suchen prüfen, ändern und eigene "
                                     "Suchbegriffe ergänzen. Erst danach werden die Arbeiten bewertet.")
    batch1 = b_go.checkbox(
        "Sparmodus: Treffer als Sammelauftrag bewerten (etwa ein Drittel günstiger, dauert meist bis zu einer Stunde länger)",
        value=bool(proj.get("batch_step1")),
        help="Die Bewertung der Suchtreffer und die Leitliniensuche gehen gesammelt an die KI und kosten dann die "
             "Hälfte. Die Aufträge und die Qualität sind dieselben wie im direkten Ablauf, nur das Ergebnis kommt "
             "später: meist innerhalb einer Stunde, spätestens nach 24 Stunden. Sie können das Fenster schließen. "
             "Speichern Sie vorher den Zwischenstand; damit findet die App den Sammelauftrag auch nach einem "
             "Neustart wieder – auch wenn Sie danach ohne Sparmodus weitermachen. Die Ersparnis ist eine "
             "Schätzung und wird nach den ersten Läufen nachgemessen.")
    go1 = b_go.form_submit_button("🔍 Literatursuche starten" + (" (zuerst Suchplan)" if proj.get("review_plan", True) else ""))

if proj.get("search_window"):
    _w = [date.fromisoformat(x) for x in proj["search_window"]]
    st.caption(f"📅 Suchzeitraum manuell: {_w[0].strftime('%m-%Y')} bis {_w[1].strftime('%m-%Y')}")
    _ww = recommend.window_warning(proj["search_window"], (proj.get("prior") or {}).get("names"))
    if _ww:
        st.warning("⚠️ " + _ww + " Ändern Sie ihn über „📅 Suchzeitraum“ und starten Sie die Literatursuche neu.")

if go1:
    llm = get_llm()
    if llm and not email.strip():
        st.warning("Bitte eine E-Mail-Adresse eingeben.")
    elif llm and not (toc_file or proj.get("toc") or proj.get("toc_from_merge")):
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
        toc_text = extract_text(toc_file.name, toc_file.read()) if toc_file else \
            (proj.get("toc_from_merge") or {}).get("text", "")
        guide_text = extract_text(guide_file.name, guide_file.read()) if guide_file else ""
        template = (template_file.name, template_file.read()) if template_file else None
        if prior_files:
            proj["prior"] = prior.build_prior({f.name: extract_text(f.name, f.read()) for f in prior_files})

        proj["review_plan"] = bool(review_plan)
        proj["batch_step1"] = _batch1 = bool(batch1)

        def step1(log):
            pipeline.run_intake(llm, proj, toc_text, guide_text, template, log)
            pipeline.run_search_plan(llm, proj, log)
            if any("ranking" in t for t in proj["topics"].values()):
                pipeline.apply_prior(proj, log)  # Vorjahresabgleich gilt sofort für bereits bewertete Themen
                pipeline.fit_to_length(proj, log)
            if proj["review_plan"]:
                return  # Suchplan anzeigen; die Bewertung startet die Autorin danach selbst
            pipeline.run_recommendations(llm, proj, log, batch=_batch1)
            pipeline.run_guideline_search(llm, proj, log)
            pipeline.run_guideline_needs(llm, proj, log)

        run_with_status("Schritt 1 läuft", step1, llm)

if proj.get("toc"):
    toc = proj["toc"]
    st.subheader(f"Kapitel {toc.get('chapter_number', '')} {toc.get('chapter_title', '')}".strip())
    rules = proj.get("rules") or {}
    if rules:
        with st.expander("📋 Erkannte Vorgaben aus den Hinweisen für Autoren"):
            st.json({k: v for k, v in rules.items() if v and not k.startswith("_")})
    _ml = proj.get("manual_limits") or {}
    _strict = not (proj.get("limit_tolerance") or pipeline.LIMIT_TOLERANCE).get("value")
    _strict_new = st.checkbox(
        "Keine Toleranz: Seitenzahl, Literaturstellen und Publikationszahl dürfen nicht überschritten werden",
        value=_strict, key="strict_limits", disabled=_running,
        help="Ohne Häkchen dürfen Seiten und Literaturstellen um bis zu 10 überschritten werden. Gilt für die "
             "Zahlen oben (bzw. die Hinweise für Autoren) und wirkt sofort, ohne Schritt 1 neu zu starten.")
    if _strict_new != _strict:
        pipeline.set_strict_limits(proj, _strict_new)
        proj["check_ok"] = False  # Publikationszahl und Umfang in Schritt 2 neu prüfen
        rules = proj.get("rules") or {}
    _cnt = dict(proj.get("tier_counts") or A.DEFAULT_TIER_COUNTS)
    st.caption("Verteilung der Würdigungslängen (Anzahl Publikationen, von oben nach Studienqualität, Priorität "
               "und Bedeutung vergeben). Gibt es mehr Spitzenstudien, werden auch diese lang gewürdigt; dafür "
               "entfallen am unteren Ende die schwächsten Studien. Das Seitenlimit gilt zusätzlich.")
    m1, m2, m3, m4 = st.columns(4)
    _cnt_new = {
        "long": int(m1.number_input("Lang (höchstens 1 Seite, Kommentar und Fazit)", 0, 100,
                                    int(_cnt.get("long", 3)), step=1, disabled=_running, key="cnt_long")),
        "mid": int(m2.number_input("Mittel (½ Seite, Fazit)", 0, 200,
                                   int(_cnt.get("mid", 8)), step=1, disabled=_running, key="cnt_mid")),
        "short": int(m3.number_input("Kurz (⅓ Seite)", 0, 300,
                                     int(_cnt.get("short", 20)), step=1, disabled=_running, key="cnt_short")),
        "news": int(m4.number_input("Kurznachricht (0,2 Seite)", 0, 300,
                                    int(_cnt.get("news", 19)), step=1, disabled=_running, key="cnt_news")),
    }
    if _cnt_new != _cnt:
        proj["tier_counts"] = _cnt_new
        pipeline.apply_manual_limits(proj)
        proj["check_ok"] = False  # Publikationszahl und Umfang in Schritt 2 neu prüfen
        rules = proj.get("rules") or {}
    if _ml.get("pages") or _ml.get("references") or _ml.get("publications"):
        st.caption(f"Manuelle Vorgaben ({pipeline.tolerance_note(proj)}): "
                   f"Obergrenze {rules.get('max_pages', 0) - 1 if rules.get('max_pages') else '–'} Seiten ohne "
                   f"Inhaltsverzeichnis, {rules.get('max_references') or '–'} Literaturstellen"
                   + (f", höchstens {_ml['publications']} gewürdigte Publikationen." if _ml.get("publications") else "."))
    if proj.get("prior"):
        _pr = proj["prior"]
        _n_ex = len({p for t in proj["topics"].values() for p in t.get("prior_excluded", {})})
        _pc1, _pc2 = st.columns([4, 1])
        _pc1.caption(f"📕 Vorjahresmanuskript: {', '.join(_pr.get('names', []))} · "
                     f"{len(_pr.get('dois', []))} DOI und {len(_pr.get('pmids', []))} PMID erkannt, zusätzlich "
                     f"Abgleich über den Titel · {_n_ex} bereits enthaltene Arbeit(en) nicht empfohlen.")
        _pw = prior.warning(_pr, _n_ex, proj.get("search_window") or proj.get("plan_window"))
        if _pw:
            st.warning("⚠️ " + _pw + " Mit „Abgleich entfernen“ kommen die Arbeiten ohne neue Kosten zurück.")
        if _pc2.button("Abgleich entfernen", key="prior_remove", disabled=_running):
            proj.pop("prior")
            proj.pop("prior_warning", None)
            pipeline.apply_prior(proj)
            pipeline.fit_to_length(proj)
            st.rerun()
    if any(t.get("ranking") for t in proj["topics"].values()):
        _foc = bool(proj.get("focus_conservative"))
        _foc_new = st.checkbox(
            "Interventionelle Studien abwerten (nur, wenn es zu diesem Thema ein eigenes interventionelles Kapitel gibt)",
            value=_foc, key="focus_conservative", disabled=_running,
            help="Studien zu Katheter, Stent, Ballon, Bypass, Thrombektomie oder Devices werden dann nicht mehr "
                 "empfohlen und stehen nur noch in der Reserve. Sinnvoll z. B. für „pAVK konservativ“, wenn "
                 "„pAVK interventionell“ ein eigenes Kapitel ist. Die Einordnung kostet einmalig wenige Cent.")
        if _foc_new != _foc:
            proj["focus_conservative"] = _foc_new
            _flm = get_llm() if _foc_new and pipeline.invasive_pending(proj) else None
            if _foc_new and pipeline.invasive_pending(proj) and not _flm:
                proj["focus_conservative"] = False
            else:
                def _refocus(log):
                    if _flm:
                        pipeline.classify_invasive(_flm, proj, log)
                    pipeline.select_chapter(proj, log)
                    pipeline.fit_to_length(proj, log)
                run_with_status("Auswahl wird angepasst", _refocus, _flm)
        _ext = pipeline.extension_pending(proj)
        if _ext["widen"] or _ext["followup"]:
            _parts = []
            if _ext["widen"]:
                _parts.append(f"{len(_ext['widen'])} Themen mit vielen Treffern: 100 statt 60 Kandidaten")
            if _ext["followup"]:
                _parts.append(f"Nachsuche nach Folgepublikationen zu {_ext['followup']} guten Studien")
            _usd = 0.5 * len(_ext["widen"]) + (0.5 if _ext["followup"] else 0)
            _ec1, _ec2 = st.columns([3, 1])
            _ec1.caption("🔁 Erweiterte Suche verfügbar: " + "; ".join(_parts) + ". Bereits bewertete Arbeiten "
                         "werden nicht erneut bewertet.")
            if _ec2.button(f"Suche erweitern (ca. {max(_usd, 0.5):.0f}–{max(_usd * 2, 1):.0f} USD)",
                           key="extend_search", disabled=_running):
                _elm = get_llm()
                if _elm:
                    run_with_status("Suche wird erweitert", lambda log: pipeline.run_recommendations(_elm, proj, log),
                                    _elm)
    if proj.get("template"):
        st.caption(f"Formatvorlage: {proj['template']['name']} · erkannte med-update-Formatvorlagen: "
                   f"{', '.join(proj['template'].get('med_update_styles', [])) or 'keine'}")

    _frame = proj.get("search_frame")
    _stopics = [(tid, t) for tid, t in proj["topics"].items()
                if t.get("enabled", True) and not t.get("varia") and not t.get("safety")]
    if _frame:
        _pending = [tid for tid, t in _stopics if t.get("searchable") is not False
                    and ("recommended" not in t or pipeline.rank_failed(t)
                         or t.get("plan_rev", 0) != t.get("ranked_rev", 0))]
        with st.expander(f"🔎 Suchplan prüfen – {len(_stopics)} Themen"
                         + (f", {len(_pending)} noch nicht bewertet" if _pending else ""), expanded=bool(_pending)):
            _st = {"methode": "nach einer Methode", "krankheit": "nach einer Erkrankung", "gemischt": "gemischt"}
            st.markdown(f"**Kapitel:** {_frame.get('chapter_en', '')} · gegliedert "
                        f"{_st.get(_frame.get('structure'), '?')}. {_frame.get('structure_note', '')}")
            _anchor = st.text_area("Kapitel-Anker (gemeinsamer Suchbegriff, in den Themensuchen bereits enthalten; "
                                   "wird für das Sicherheitsnetz verwendet)", value=_frame.get("anchor_query", ""),
                                   height=80, disabled=_running, key="plan_anchor")
            if _anchor != _frame.get("anchor_query", ""):
                _frame["anchor_query"] = _anchor
            _fc = _frame.get("counts") or {}
            st.caption("Sicherheitsnetz (kapitelweit, findet auch Themen außerhalb des Inhaltsverzeichnisses): "
                       f"hohe Evidenz {_fc.get('netz_evidenz', '?')} · Top-Journale {_fc.get('netz_journale', '?')} · "
                       f"Anker allein {_fc.get('netz_anker', '?')} Treffer"
                       + (f" · Zeitraum {proj['plan_window'][0]} bis {proj['plan_window'][1]}" if proj.get("plan_window") else ""))
            _names = {"praezise": "präzise", "breit": "breit", "hohe_evidenz": "hohe Evidenz", "eigene": "eigene"}
            for tid, t in _stopics:
                _head = f"{t.get('number', '')} {t['title']}".strip()
                if t.get("searchable") is False:
                    st.markdown(f"**{_head}** – keine eigene Suche (Überschrift nicht recherchierbar, wird über das "
                                "Sicherheitsnetz abgedeckt)")
                    if st.checkbox("trotzdem eigene Suche anlegen", key=f"forcesearch_{tid}", disabled=_running):
                        t["searchable"] = True
                        t.pop("recommended", None)
                    continue
                _cnt = t.get("plan_counts") or {}
                _hits = " / ".join(str(_cnt.get(q["name"], "?")) for q in (t.get("plan") or {}).get("queries", []))
                with st.popover(f"{_head} · {_hits or 'noch kein Suchplan'} Treffer", use_container_width=True):
                    st.caption(f"Englisch: {t.get('title_en', '')}")
                    for i, q in enumerate((t.get("plan") or {}).get("queries", [])):
                        _new = st.text_area(f"{_names.get(q['name'], q['name'])} · {_cnt.get(q['name'], '?')} Treffer",
                                            value=q["query"], height=90, disabled=_running,
                                            key=f"q_{tid}_{i}_{t.get('plan_rev', 0)}")
                        if _new != q["query"]:
                            q["query"] = _new
                            t["plan_rev"] = t.get("plan_rev", 0) + 1
                    _man = st.text_input(f"Eigene Suchbegriffe (zusätzlich) · {_cnt.get('eigene', '–')} Treffer",
                                         value=t.get("manual_query", ""), disabled=_running, key=f"man_{tid}",
                                         help="Wird zusätzlich zu den KI-Suchen in PubMed gesucht, z.B. "
                                              "„EVAR AND sac embolization“ oder ein Studienname.")
                    if _man != t.get("manual_query", ""):
                        t["manual_query"] = _man
                        t["plan_rev"] = t.get("plan_rev", 0) + 1
            _b1 = st.checkbox(
                "Sparmodus: Treffer als Sammelauftrag bewerten (etwa ein Drittel günstiger, dauert meist bis zu "
                "einer Stunde länger)", value=bool(proj.get("batch_step1")),
                key=f"batch_step1_plan_{int(bool(proj.get('batch_step1')))}", disabled=_running,
                help="Die Bewertung der Suchtreffer und die Leitliniensuche gehen gesammelt an die KI und kosten "
                     "dann die Hälfte. Aufträge und Qualität sind dieselben wie im direkten Ablauf, nur das Ergebnis "
                     "kommt später: meist innerhalb einer Stunde, spätestens nach 24 Stunden. Speichern Sie vorher "
                     "den Zwischenstand; damit findet die App den Sammelauftrag auch nach einem Neustart wieder – "
                     "auch wenn Sie danach ohne Sparmodus weitermachen.")
            proj["batch_step1"] = bool(_b1)
            p1, p2 = st.columns(2)
            if p1.button("🔢 Treffer neu zählen", disabled=_running, key="plan_recount"):
                _plm = get_llm()
                if _plm:
                    run_with_status("Suchplan wird aktualisiert",
                                    lambda log: pipeline.run_search_plan(_plm, proj, log), _plm)
            _n = max(len(_pending), 1)
            _f1 = STEP1_BATCH_FACTOR if _b1 else 1.0
            _dec = 0 if _n >= 10 else 1
            if p2.button("✅ Literatur bewerten und Leitlinien recherchieren (ca. "
                         f"{f'{0.4 * _n * _f1:.{_dec}f}'.replace('.', ',')}–"
                         f"{f'{0.8 * _n * _f1:.{_dec}f}'.replace('.', ',')} USD)",
                         disabled=_running or not _pending, key="plan_go", type="primary"):
                _plm = get_llm()
                if _plm:
                    def _rank_all(log, _batch=bool(_b1)):
                        pipeline.run_search_plan(_plm, proj, log)
                        pipeline.run_recommendations(_plm, proj, log, batch=_batch)
                        pipeline.run_guideline_search(_plm, proj, log)
                        pipeline.run_guideline_needs(_plm, proj, log)
                    run_with_status("Literatur wird bewertet", _rank_all, _plm)
            st.caption("Die Kosten sind eine Schätzung (Bewertung der Treffer und Leitlinienrecherche je Thema). "
                       "Geänderte Suchen werden beim nächsten Bewerten neu ausgeführt, unveränderte Themen nicht.")
        _net = proj.get("safety_net")
        if _net:
            st.caption(f"🛟 Sicherheitsnetz: {_net.get('n_new', 0)} zusätzliche Arbeiten bewertet, "
                       f"{_net.get('assigned', 0)} einem Thema zugeordnet, {_net.get('orphans', 0)} als mögliches "
                       "neues Thema (ganz unten, zunächst nicht im Manuskript).")

    lp = proj.get("length_plan") or {}
    plan_tiers = lp.get("tiers", {})
    if lp:
        st.caption(f"Empfohlen werden die besten Arbeiten des Kapitels bis zur Zielzahl (Priorität A zuerst, dann "
                   f"nach Score); jede Arbeit steht in dem Thema, zu dem sie am besten passt. Geschätzter Umfang ≈{lp['pages']} Seiten"
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
            if rec_ids is None or "window" not in t:  # noch nicht recherchiert oder fehlgeschlagen
                if not t.get("rec_error"):
                    st.info("Noch keine Literaturempfehlung.")
                continue
            if not rec_ids:
                st.warning("Keine Empfehlung für dieses Thema. Bei Bedarf eine Arbeit aus der Reserve anhaken.")
            st.caption(f"Zeitraum {t['window'][0]} bis {t['window'][1]} · {t['n_candidates']} Kandidaten bewertet · "
                       f"Treffer je Suchstrategie: {t.get('query_hits')}")
            if t.get("prior_excluded"):
                with st.popover(f"📕 {len(t['prior_excluded'])} schon im Vorjahresmanuskript"):
                    for _p, _e in t["prior_excluded"].items():
                        _r = proj["records"].get(_p, {})
                        st.markdown(f"- {_r.get('title', _p)} — {_r.get('journal', '')} {_r.get('year', '')} "
                                    f"(erkannt über {_e.get('why', '')})")
            if t.get("background"):
                with st.popover(f"📖 {len(t['background'])} Hintergrundliteratur (wird nicht gewürdigt)"):
                    st.caption("Leitlinien, Konsensuspapiere und Übersichten aus dem Suchzeitraum zum Nachlesen; "
                               "sie stehen auch in der Auswahlliste und können im Kommentar zitiert werden.")
                    for _p in t["background"]:
                        _r = proj["records"].get(_p, {})
                        st.markdown(f"- {_r.get('title', _p)} — {_r.get('journal', '')} {_r.get('year', '')}")
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
                if it.get("via"):
                    label += "  \n🛟 über das Sicherheitsnetz gefunden"
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
        st.markdown("**Auswahlliste für die Autoren** (zum Versenden per Mail): Die Autoren haken Publikationen "
                    "an, ändern die Reihenfolge, laden PDFs und Leitlinien hoch und speichern alles als ZIP-Paket. "
                    "Das Paket laden Sie in Schritt 2 hoch.")
        if any(t.get("guidelines") and "gl_need" not in t
                       for t in proj["topics"].values() if t.get("enabled", True)):
            st.caption("Die Liste fragt noch alle fehlenden Leitlinien bei den Autoren an. Mit dem Knopf bestimmt die "
                       "KI, welche Leitlinien die Würdigungen wirklich verbessern; nur diese werden angefragt.")
            if st.button("🔎 Benötigte Leitlinien bestimmen (ca. 0,01 USD je Unterkapitel)", disabled=_running,
                         key="gl_need_btn"):
                _gl_llm = get_llm(CHECK_BUDGET_USD)
                if _gl_llm:
                    run_with_status("Benötigte Leitlinien werden bestimmt",
                                    lambda log: pipeline.run_guideline_needs(_gl_llm, proj, log), _gl_llm)
        a1, a2 = st.columns(2)
        a1.download_button("⬇️ Auswahlliste als ZIP (für Mail empfohlen)", data=selection_sheet.build_sheet_zip(proj),
                           file_name=selection_sheet.sheet_filename(proj, "zip"), mime="application/zip")
        a2.download_button("⬇️ Auswahlliste als HTML-Datei", data=selection_sheet.build_sheet(proj),
                           file_name=selection_sheet.sheet_filename(proj), mime="text/html")
        if selection_sheet.link_available():
            st.markdown("**Auswahlliste als Link** (öffnet sich auf Handy, Tablet und Computer im Browser):")
            l1, l2 = st.columns(2)
            _link = proj.get("sheet_link")
            if l1.button("🔗 Link aktualisieren" if _link else "🔗 Link für Autoren erstellen", key="sheet_link_btn"):
                selection_sheet.publish_sheet(proj)
                _link = proj["sheet_link"]
            if _link:
                st.code(_link["url"], language=None)
                st.caption(f"Stand der Liste: {_link['updated']}. Diesen Link per Mail an die Autoren schicken. Nach "
                           "Änderungen in Schritt 1 „Link aktualisieren“ drücken; der Link bleibt gleich. Wer den "
                           "Link hat, kann die Liste sehen.")
                if l2.button("🗑️ Link löschen", key="sheet_link_del"):
                    selection_sheet.unpublish_sheet(proj)
                    st.rerun()
        else:
            st.caption("Ein Link zur Auswahlliste (auch fürs Handy) lässt sich nur in der App auf dem eigenen "
                       "Hetzner-Server erstellen.")

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
    # Je Publikation: kritische Würdigung 1,5–2,0 USD (im Sparmodus rund die Hälfte) plus Kürzen, Übersetzen und
    # Stil 0,2–0,3 USD; dazu je Abschnitt Kommentare, Überleitungen und Prüfungen (Kostenprüfung vom 13.11.2026,
    # hochgerechnet aus einem Projekt mit 28 Würdigungen – nach den ersten Läufen durch Messwerte ersetzen)
    st.info(f"Grobe Schätzung der KI-Kosten für Schritt 2: etwa {len(wanted) * 1.7 + n_sec * 0.3 + 1:.0f}–"
            f"{len(wanted) * 2.3 + n_sec * 0.6 + 3:.0f} USD für {len(wanted)} Publikationen, im Sparmodus etwa "
            f"{len(wanted) * 1.0 + n_sec * 0.3 + 1:.0f}–{len(wanted) * 1.3 + n_sec * 0.6 + 3:.0f} USD (Grundlage: "
            "Kostenprüfung vom 13.11.2026, hochgerechnet aus einem Projekt; der größte Teil entfällt auf die "
            "kritischen Würdigungen). Die tatsächlichen Kosten misst die App und zeigt sie je Arbeitsschritt in "
            "der Seitenleiste an; das Kostenlimit unten hält den Lauf rechtzeitig an.")
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

    # 2·0. Bearbeitete Auswahlliste der Autoren übernehmen
    with st.form("step2_package"):
        pkg_file = st.file_uploader("Bearbeitete Auswahlliste der Autoren (ZIP-Paket aus „Auswahl speichern“)",
                                    type=["zip"])
        st.caption("Übernimmt Auswahl, Reihenfolge, Unterkapitel, PDFs und Leitlinien der Autoren und prüft danach "
                   "die Themenzuordnung. Die Festlegungen der Autoren ändert die KI nicht. Kosten ca. 0,2–1 USD.")
        go_pkg = st.form_submit_button("📥 Auswahl der Autoren übernehmen und prüfen", type="primary")
    if go_pkg and pkg_file is not None:
        llm = get_llm(CHECK_BUDGET_USD)
        if llm:
            try:
                res = pipeline.apply_selection_package(proj, pkg_file.getvalue())
            except ValueError as ex:
                st.error(str(ex))
                res = None
            if res:
                r = res["report"]
                if r["other_project"]:
                    st.warning("Das Paket wurde aus einer Auswahlliste eines anderen Projekts erstellt. Übernommen "
                               "wurden nur Publikationen, die in diesem Projekt vorkommen.")
                st.session_state["pkg_report"] = r
                run_with_status("Auswahl der Autoren wird übernommen",
                                lambda log: pipeline.check_assignment(llm, proj, res["uploads"], log,
                                                                      forced_topics=res["forced_topics"]), llm)
    if st.session_state.get("pkg_report"):
        r = st.session_state["pkg_report"]
        st.success(f"Auswahl der Autoren{' (' + r['edited_by'] + ')' if r['edited_by'] else ''} übernommen: "
                   f"{r['selected']} Publikationen angehakt, davon {r['moved']} in ein anderes Unterkapitel "
                   f"verschoben, {r['pdfs']} PDFs, {r['extra']} zusätzliche Publikationen, {r['guidelines']} "
                   "Leitlinien.")
        if r["pdf_warn"]:
            st.warning("Diese PDFs passen evtl. nicht zur Publikation, bitte prüfen: " + "; ".join(r["pdf_warn"][:10]))

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
        opt_trans = o2.checkbox("Überleitungen zwischen den Studien", value=True,
                                help="Immer vor der ersten Publikation jedes Themas, vor jeder gemeinsamen Würdigung "
                                     "und vor jeder langen Würdigung; sonst gelegentlich (höchstens eine je drei "
                                     "Publikationen). Ersetzt den State of the Art.")
        opt_style = o3.checkbox("An meinen Schreibstil anpassen", value=True)
        opt_joint = st.checkbox("Gemeinsame Würdigung für Publikationen mit gleicher Fragestellung (2–5 je Gruppe)",
                                value=True, help="Wie Schritt 4B in MyLuo. Die Einzelwürdigungen werden zu einer "
                                                 "vergleichenden Würdigung zusammengeführt.")
        opt_own = st.checkbox("Inhaltliche Rückbezüge auf meine früheren Manuskripte (höchstens 2, nur wo sinnvoll)",
                              value=True, help="Nur wenn eigene Manuskripte hochgeladen sind. Ein zusätzlicher KI-Aufruf.")
        _cmp = proj.get("compact_opts") or {}
        k1, k2 = st.columns(2)
        opt_cstats = k1.checkbox("Statistik kompakt: nur die wesentlichen Punkte", value=bool(_cmp.get("stats")),
                                 help="Kurz und mittel: 1 Satz, lang: 2 Sätze, jeweils plus ein Satz "
                                      "zur statistischen Qualität. Bereits gekürzte Würdigungen werden bei einer "
                                      "Änderung neu gekürzt (ca. 0,3 USD je Publikation).")
        opt_ccomm = k2.checkbox("Kommentar kompakt: nur die wichtigste Einordnung", value=bool(_cmp.get("comment")),
                                help="Lang: 2 Sätze. Bereits gekürzte "
                                     "Würdigungen werden bei einer Änderung neu gekürzt (ca. 0,3 USD je Publikation).")
        opt_batch = st.checkbox(
            "Sparmodus: kritische Würdigungen als Sammelauftrag zum halben Preis (Ergebnis meist innerhalb einer "
            "Stunde, spätestens nach 24 Stunden)", value=bool(proj.get("batch_step2")),
            help="Die kritischen Würdigungen sind der teuerste Teil (rund 70 % der Kosten). Im Sparmodus gehen sie "
                 "gesammelt an die KI und kosten die Hälfte; Vorgabe, Websuche und Qualität sind dieselben wie im "
                 "direkten Ablauf. Alles Weitere (Kürzen, Übersetzen, Stil, Zusammensetzen) läuft danach wie "
                 "gewohnt. Sie können das Fenster schließen; die App holt das Ergebnis ab. Speichern Sie vorher "
                 "den Zwischenstand; damit findet die App den Sammelauftrag auch nach einem Neustart wieder und "
                 "nichts wird doppelt bezahlt – auch wenn Sie danach ohne Sparmodus weitermachen. Das Kostenlimit "
                 "gilt schon beim Einreichen: Die App reicht nur so viele Würdigungen ein, wie nach Schätzung "
                 "(ca. 0,9 USD je Würdigung) hineinpassen.")
        opt_lean = st.checkbox(
            "Schlanke Würdigung für Kurznachrichten (ohne Websuche; bitte erst nach dem Vergleich unten einschalten)",
            value=bool(proj.get("lean_news")),
            help="Eine Kurznachricht besteht aus höchstens drei Sätzen: Ziel, Design, Patientenzahl und die "
                 "Hauptaussage. Dafür prüft die schlanke Würdigung Methodik, Validität und Schlussfolgerungen der "
                 "Autoren wie bisher; Websuche, Leitlinienbezug und Kommentar entfallen. Gilt nur für einzeln "
                 "stehende, als Kurznachricht geplante Publikationen ohne vorhandene Würdigung. Wird eine solche "
                 "Publikation später länger geplant oder gemeinsam gewürdigt, erstellt die App die vollständige "
                 "Würdigung nach. Ohne Häkchen werden vorhandene schlanke Würdigungen beim nächsten Lauf durch "
                 "vollständige ersetzt (voller Preis je Würdigung).")
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
                                          "ein erneuter Start setzt dort fort. Im Sparmodus reicht die App nur so "
                                          "viele Würdigungen ein, wie nach Schätzung ins Limit passen (ca. 0,9 USD "
                                          "je Würdigung); für einen vollständigen Lauf das Limit entsprechend "
                                          "höher setzen.")
        n_old = sum(1 for d in proj.get("pubs", {}).values()
                    if d.get("short_en") and d.get("short_v", 1) < pipeline.SHORT_VERSION)
        opt_reshorten = st.checkbox(
            f"Bereits gekürzte Würdigungen neu kürzen (verständlichere Methodik/Statistik, abwechslungsreiches "
            f"Fazit, kürzere Sätze; {n_old} betroffen, ca. {0.3 * n_old:.0f} USD)", value=False, disabled=not n_old,
            help="Die kritischen Würdigungen selbst werden nicht neu erstellt, nur Kürzung, Übersetzung und Stil.")
        go2 = st.form_submit_button("✍️ Manuskript automatisch erstellen", disabled=not ready, type="primary")

    if go2 and ready:
        proj["compact_opts"] = {"stats": bool(opt_cstats), "comment": bool(opt_ccomm)}
        proj["batch_step2"], proj["lean_news"] = bool(opt_batch), bool(opt_lean)
        llm = get_llm(float(opt_budget))
        if llm:
            run_with_status("Manuskript wird erstellt", lambda log: pipeline.run_manuscript(llm, proj, log, {
                "sota": False, "transitions": opt_trans, "style": opt_style, "trial": opt_trial,
                "trial_topic": opt_trial_topic, "own_refs": opt_own, "joint": opt_joint,
                "reshorten": opt_reshorten, "compact_stats": opt_cstats, "compact_comment": opt_ccomm,
                "batch": bool(opt_batch), "lean_news": bool(opt_lean)}), llm)

    # 2c. Vergleich für die schlanke Würdigung (Entscheidungshilfe, bevor der Schalter oben genutzt wird)
    with st.expander("🧪 Vergleich: Kurznachricht aus vollständiger und aus schlanker Würdigung",
                     expanded=bool(proj.get("lean_test"))):
        st.caption("Für drei als Kurznachricht geplante Publikationen erstellt die App die Kurznachricht zweimal: "
                   "einmal aus der vollständigen kritischen Würdigung und einmal aus der schlanken. In der "
                   "Word-Datei stehen beide Fassungen ohne Kennzeichnung nebeneinander, die Auflösung und die "
                   "gemessenen Kosten stehen auf der letzten Seite. Die vollständigen Würdigungen bleiben im "
                   "Projekt und werden später nicht noch einmal bezahlt. Wie beim Manuskript muss vorher die "
                   "Themenzuordnung geprüft sein.")
        if st.button("🧪 Vergleich für 3 Publikationen erstellen (ca. 6–7 USD; mit vorhandenen Würdigungen ca. 1 USD)",
                     disabled=_running or not ready, key="lean_test_btn"):
            _tlm = get_llm(LEAN_TEST_BUDGET_USD)
            if _tlm:
                run_with_status("Vergleich wird erstellt",
                                lambda log: pipeline.run_lean_comparison(_tlm, proj, log), _tlm)
        _cmp_docx = pipeline.lean_comparison_docx(proj)
        if _cmp_docx:
            st.download_button("⬇️ Vergleich als Word-Datei", _cmp_docx,
                               f"Vergleich_Kurznachrichten_{date.today().isoformat()}.docx", DOCX_MIME,
                               key="lean_test_dl")
            _tc = (proj["lean_test"].get("costs") or {})
            if _tc.get("full_usd") is not None and _tc.get("lean_usd") is not None:
                st.caption(f"Gemessen: vollständige Würdigung ≈ {usd(_tc['full_usd'])} USD, schlanke Würdigung ≈ "
                           f"{usd(_tc['lean_usd'])} USD je Publikation (direkter Aufruf).")

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
