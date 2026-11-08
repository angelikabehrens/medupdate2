"""Auswahlliste für Autoren (Angelika 2026-11-03): eine eigenständige HTML-Datei, die per Mail verschickt wird.
Die Autoren haken Publikationen an, verschieben sie (auch in andere Unterkapitel), laden die PDFs direkt hinter
der Literaturstelle hoch (mit Prüfung), ergänzen weitere Publikationen und Leitlinien und speichern alles als
ZIP-Paket. Das Paket wird in Schritt 2 der App hochgeladen (pipeline.apply_selection_package).

Die HTML-Datei braucht keine Installation; die PDF-Prüfung nutzt pdf.js (cdnjs). Ohne Internet wird die
Prüfung in der App nachgeholt."""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import zipfile
from datetime import date, datetime
from pathlib import Path

FORMAT = "myluo-auswahl"
FORMAT_VERSION = 1


def _authors(rec: dict) -> str:
    a = rec.get("authors") or []
    return ", ".join(a[:3]) + (" et al." if len(a) > 3 else "")


def _source(rec: dict) -> str:
    src = f"{rec.get('journal', '')} {rec.get('year', '')}".strip()
    if rec.get("volume"):
        src += f";{rec['volume']}" + (f"({rec['issue']})" if rec.get("issue") else "")
    if rec.get("pages"):
        src += f":{rec['pages']}"
    return src


def target_count(proj: dict) -> tuple[int, str]:
    """Wie viele Publikationen ausgewählt werden sollen: Höchstzahl aus Schritt 1, sonst die Summe der
    Würdigungslängen (lang + mittel + kurz + Kurznachricht)."""
    rules = proj.get("rules") or {}
    n = int(rules.get("max_publications") or (proj.get("manual_limits") or {}).get("publications") or 0)
    if n:
        return n, "Höchstzahl gewürdigter Publikationen laut Schritt 1"
    from .appraisal import DEFAULT_TIER_COUNTS
    cnt = rules.get("tier_counts") or proj.get("tier_counts") or DEFAULT_TIER_COUNTS
    return int(sum(int(v) for v in cnt.values())), "Summe der Würdigungslängen laut Schritt 1"


def sheet_data(proj: dict) -> dict:
    """Alle Angaben, die die HTML-Datei braucht (Themen in Manuskript-Reihenfolge mit ihren Publikationen)."""
    from . import pipeline  # spät importieren (Zirkelbezug)
    cur = pipeline.current_topics(proj)
    plan = (proj.get("length_plan") or {}).get("tiers", {})
    rankings = {}
    for t in proj["topics"].values():
        for p, it in (t.get("ranking") or {}).items():
            rankings.setdefault(p, it)
    topics, seen = [], set()
    for tid, t in proj["topics"].items():
        if not t.get("enabled", True):
            continue
        rk = t.get("ranking") or {}
        chosen = [p for p, x in pipeline._pub_list(proj) if x == tid]
        others = [p for p in dict.fromkeys(list(t.get("recommended", [])) + list(t.get("reserve", [])))
                  if p not in chosen and p not in cur]  # woanders ausgewählte Arbeiten stehen dort
        items = []
        for p in chosen + others:
            if p in seen:
                continue
            seen.add(p)
            rec, d = proj["records"].get(p, {}), proj["pubs"].get(p, {})
            it = rk.get(p) or rankings.get(p) or {}
            status = ("hochgeladen" if d.get("extra") else
                      "Empfehlung" if p in t.get("recommended", []) else
                      "Reserve" if p in t.get("reserve", []) else "verschoben")
            items.append({
                "id": p, "checked": p in chosen, "title": rec.get("title", p), "authors": _authors(rec),
                "first_author": ((rec.get("authors") or [""])[0].split(" ") or [""])[0],
                "source": _source(rec), "doi": rec.get("doi", ""), "pmid": p if p.isdigit() else "",
                "priority": it.get("priority", ""), "quality": it.get("quality", ""),
                "design": it.get("study_design", ""), "rationale": it.get("rationale", ""),
                "tier": plan.get(p) or (proj.get("tiers") or {}).get(p, ""), "status": status,
                "has_fulltext": bool(d.get("full_text")), "is_oa": bool(rec.get("is_oa")),
                "upload_name": d.get("upload_name", ""),
            })
        topics.append({"id": tid, "number": t.get("number", ""), "title": t["title"],
                       "section": f"{t.get('section_number', '')} {t.get('section_title', '')}".strip()
                       if t.get("section_title") and t.get("section_title") != t["title"] else "",
                       "items": items,
                       "guidelines": [{"title": g.get("title", ""), "society": g.get("society", ""),
                                       "year": g.get("year", ""), "loaded": bool(g.get("text"))}
                                      for g in t.get("guidelines") or []]})
    n, why = target_count(proj)
    toc = proj.get("toc") or {}
    return {"format": FORMAT, "version": FORMAT_VERSION, "project_id": proj.get("id", ""),
            "chapter": f"{toc.get('chapter_number', '')} {toc.get('chapter_title', '')}".strip(),
            "created": date.today().isoformat(), "target": n, "target_note": why, "topics": topics}


def build_sheet(proj: dict) -> bytes:
    data = json.dumps(sheet_data(proj), ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.replace("__DATA__", data).encode("utf-8")


def build_sheet_zip(proj: dict) -> bytes:
    """Die HTML-Datei zusätzlich als ZIP (manche Mailprogramme blockieren HTML-Anhänge)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(sheet_filename(proj), build_sheet(proj))
    return buf.getvalue()


def sheet_filename(proj: dict, ext: str = "html") -> str:
    title = (proj.get("toc") or {}).get("chapter_title", "") or "Kapitel"
    slug = re.sub(r"[^A-Za-z0-9ÄÖÜäöüß]+", "_", title).strip("_")[:40] or "Kapitel"
    return f"Auswahlliste_{slug}_{date.today().isoformat()}.{ext}"


# Auswahlliste als Link (Angelika 2026-11-05: auch auf dem Handy öffnen). Nur auf dem eigenen Server
# (deploy/install.sh): Caddy liefert /opt/myluo/listen unter <Adresse>/liste/ aus. Streamlit Cloud hat keinen
# dauerhaften Speicher, dort gibt es den Link nicht.
_LIST_DIR = Path(os.environ.get("MYLUO_LIST_DIR", "/opt/myluo/listen"))
_ADDRESS_FILE = Path(os.environ.get("MYLUO_ADDRESS_FILE", "/opt/myluo/ADRESSE.txt"))


def link_available() -> bool:
    return _LIST_DIR.is_dir() and os.access(_LIST_DIR, os.W_OK) and _ADDRESS_FILE.is_file()


def publish_sheet(proj: dict) -> str:
    """Legt die Auswahlliste unter einem zufälligen, nicht erratbaren Namen ab und gibt den Link zurück.
    Ein erneuter Aufruf aktualisiert dieselbe Liste; der Link bleibt gleich."""
    link = proj.get("sheet_link") or {}
    token = link.get("token") or secrets.token_urlsafe(18)
    path = _LIST_DIR / f"{token}.html"
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(build_sheet(proj))
    tmp.chmod(0o644)
    tmp.replace(path)
    url = f"{_ADDRESS_FILE.read_text().strip().rstrip('/')}/liste/{token}.html"
    proj["sheet_link"] = {"token": token, "url": url, "updated": datetime.now().strftime("%d.%m.%Y %H:%M")}
    return url


def unpublish_sheet(proj: dict) -> None:
    link = proj.pop("sheet_link", None) or {}
    if link.get("token") and re.fullmatch(r"[A-Za-z0-9_-]+", link["token"]):
        (_LIST_DIR / f"{link['token']}.html").unlink(missing_ok=True)


def read_package(data: bytes) -> tuple[dict, dict[str, bytes]]:
    """Liest das von der Auswahlliste gespeicherte ZIP-Paket: (auswahl.json, {Pfad im ZIP: Bytes})."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as ex:
        raise ValueError("Die Datei ist kein ZIP-Paket der Auswahlliste.") from ex
    names = z.namelist()
    meta_name = next((n for n in names if n.rsplit("/", 1)[-1] == "auswahl.json"), None)
    if not meta_name:
        raise ValueError("Im Paket fehlt „auswahl.json“. Bitte das mit „Auswahl speichern“ erzeugte Paket hochladen.")
    meta = json.loads(z.read(meta_name).decode("utf-8"))
    if meta.get("format") != FORMAT:
        raise ValueError("Das Paket stammt nicht aus der Auswahlliste von MyLuo Pro.")
    prefix = meta_name[: -len("auswahl.json")]
    files = {n[len(prefix):]: z.read(n) for n in names if n.startswith(prefix) and n != meta_name and not n.endswith("/")}
    return meta, files


_TEMPLATE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Auswahlliste Literatur</title>
<style>
:root{--bg:#f6f8f7;--card:#fff;--ink:#26332f;--muted:#5d6b66;--line:#d5ddd9;--green:#5b7468;--mist:#e8eeea;
--ochre:#a8781f;--ochre-tint:#f5eddc;--red:#a33a2c;--red-tint:#f8e4e1;--ok:#2f6b46;--ok-tint:#e1f0e6}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 Calibri,"Segoe UI",Arial,sans-serif}
header{position:sticky;top:0;z-index:10;background:var(--ink);color:#fff;padding:10px 16px;
display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;box-shadow:0 2px 6px rgba(0,0,0,.15)}
header h1{font:600 18px Cambria,Georgia,serif;margin:0;flex:1 1 260px}
.counter{font-size:16px;padding:6px 12px;border-radius:6px;background:var(--green);white-space:nowrap}
.counter.over{background:var(--red)}.counter.under{background:var(--ochre)}
header button,.btn{font:inherit;border:0;border-radius:6px;padding:7px 14px;cursor:pointer;background:#fff;color:var(--ink)}
header button.primary{background:var(--ochre);color:#fff;font-weight:600}
.btn.small{padding:3px 9px;font-size:13px;background:var(--mist)}
.btn.warn{background:var(--ochre);color:#fff}
main{max-width:1050px;margin:0 auto;padding:16px}
.intro{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin-bottom:16px}
.intro ol{margin:6px 0 0 18px;padding:0}
.intro label.editor{display:block;margin:0 0 10px}
.filebtn{display:inline-block;font-size:13px;border-radius:6px;padding:4px 10px;background:var(--mist);border:1px solid var(--line);cursor:pointer}
.filebtn{position:relative}.filebtn input{position:absolute;left:0;top:0;width:1px;height:1px;opacity:0;overflow:hidden}
.intro input[type=text]{font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:4px;width:260px;max-width:100%}
h2.section{font:600 19px Cambria,Georgia,serif;margin:26px 0 8px;color:var(--green)}
.topic{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:10px 0 14px;overflow:hidden}
.topic-head{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline;padding:10px 14px;background:var(--mist)}
.topic-head h3{margin:0;font:600 16px Cambria,Georgia,serif;flex:1 1 300px}
.topic-count{color:var(--muted);font-size:13px}
.list{min-height:46px;padding:6px 10px}
.list.drop{background:var(--ochre-tint);outline:2px dashed var(--ochre);outline-offset:-4px}
.empty{color:var(--muted);font-style:italic;padding:8px 6px;font-size:13px}
.pub{display:grid;grid-template-columns:22px 26px 1fr;gap:4px 8px;border:1px solid var(--line);border-radius:7px;
padding:9px 10px;margin:6px 0;background:#fff}
.pub.off{background:#f3f4f3;color:var(--muted)}
.pub.dragging{opacity:.4}
.handle{cursor:grab;color:var(--muted);font-size:18px;line-height:1;user-select:none;padding-top:1px}
.pub input[type=checkbox]{width:18px;height:18px;margin:2px 0 0}
.pub .t{font-weight:600}
.meta{font-size:13px;color:var(--muted)}
.meta a{color:var(--green)}
.why{font-size:13px;font-style:italic;color:var(--muted)}
.tag{display:inline-block;font-size:12px;padding:0 6px;border-radius:9px;background:var(--mist);color:var(--ink);margin-right:4px}
.tag.ochre{background:var(--ochre-tint);color:#7a5612}
.row{grid-column:3;display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;margin-top:4px}
.row select{font:inherit;font-size:13px;padding:2px 4px;max-width:260px}
.up{font-size:13px}
.up input{font-size:13px;max-width:240px}
.st{font-size:13px;padding:2px 8px;border-radius:5px}
.st.ok{background:var(--ok-tint);color:var(--ok)}.st.bad{background:var(--red-tint);color:var(--red)}
.st.info{background:var(--mist);color:var(--ink)}
.extra-box{border-top:1px solid var(--line);padding:8px 14px;font-size:13px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.gl{font-size:13px;color:var(--muted);padding:4px 14px 8px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:22px 0}
.panel h2{font:600 17px Cambria,Georgia,serif;margin:0 0 6px}
.gl-item{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:13px;margin:4px 0}
.pick-cur{font-size:13px;color:var(--ink);flex:1 1 200px}
.picker{position:fixed;inset:0;background:rgba(20,30,25,.45);display:none;z-index:30;align-items:flex-end;justify-content:center}
.picker.open{display:flex}
.sheet{background:#fff;width:100%;max-width:640px;max-height:85vh;border-radius:12px 12px 0 0;display:flex;flex-direction:column;box-shadow:0 -4px 20px rgba(0,0,0,.2)}
.sheet-head{padding:12px 16px;border-bottom:1px solid var(--line);font-weight:600}
.sheet-head .meta{font-weight:400;margin-top:2px;overflow-wrap:anywhere}
.sheet-list{overflow-y:auto;padding:6px 10px;-webkit-overflow-scrolling:touch}
.sheet-sec{font:600 14px Cambria,Georgia,serif;color:var(--green);margin:10px 6px 4px}
.sheet-opt{display:block;width:100%;text-align:left;font:inherit;font-size:15px;line-height:1.35;white-space:normal;overflow-wrap:anywhere;
background:#fff;border:1px solid var(--line);border-radius:7px;padding:10px 12px;margin:5px 0;cursor:pointer;color:var(--ink)}
.sheet-opt:hover{background:var(--mist)}.sheet-opt[disabled]{background:var(--mist);color:var(--muted);cursor:default}
.sheet-foot{padding:10px 16px;border-top:1px solid var(--line);text-align:right}
@media (min-width:601px){.picker{align-items:center}.sheet{border-radius:12px}}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--ink);color:#fff;
padding:10px 16px;border-radius:8px;display:none;max-width:90vw;z-index:20}
@media (max-width:600px){header{padding:8px 12px;gap:6px 8px}header h1{font-size:15px;flex-basis:100%}
.counter{font-size:13px;padding:4px 8px;flex:1 1 100%;text-align:center}header button{padding:6px 10px;font-size:13px;flex:1 1 0}
main{padding:10px}.pub{grid-template-columns:20px 22px 1fr;padding:8px}.row{grid-column:1 / -1}
.up input{max-width:100%}.btn.small{padding:6px 12px;font-size:14px}}
</style>
</head>
<body>
<header>
  <h1 id="hd">Auswahlliste</h1>
  <span class="counter" id="counter">0 von 0</span>
  <button id="openBtn" title="Eine früher gespeicherte Arbeitsliste wieder öffnen">Gespeicherte Arbeitsliste öffnen</button>
  <button class="primary" id="saveBtn">Auswahl speichern</button>
  <input type="file" id="openFile" accept=".zip" hidden>
</header>
<main>
  <div class="intro">
    <label class="editor">Bearbeitet von: <input type="text" id="editor" placeholder="Name"></label>
    <strong>So geht es:</strong>
    <ol>
      <li>Die Literaturliste ist nach Unterthemen gegliedert. Gehen Sie die Publikationen in den einzelnen Unterkapiteln durch. Zu jeder Arbeit finden Sie einen direkten Link zu PubMed („PubMed“) oder zur Zeitschrift („DOI“), jeweils anklicken. Unter der Publikation finden Sie die Qualitätsbeurteilung durch die KI. Die gelbe Markierung „Empfehlung“ weist darauf hin, dass es sich um eine relevante Publikation handelt (in der Regel multizentrische RCTs). Setzen Sie ein Häkchen bei jeder Publikation, die im Handbuch gewürdigt werden soll. Das Häkchen kennzeichnet Ihre Auswahl. Die Anzeige oberhalb der Liste zählt die ausgewählten Publikationen mit und zeigt die von med update vorgesehene Höchstzahl. Bitte wählen Sie nicht mehr Publikationen aus, als dort vorgesehen sind. Sie können Ihre Auswahl jederzeit ändern.</li>
      <li>Reihenfolge ändern: Publikation am Griff <b>⠿</b> ziehen, auch in ein anderes Unterkapitel. Alternativ (und auf dem Handy) die Pfeile oder „Verschieben nach …“ verwenden.</li>
      <li>Bitte laden Sie die PDF-Datei der Publikation direkt bei der Publikation hoch („Datei hochladen“). Die Datei wird sofort geprüft, ob es sich um die korrekte Publikation handelt.</li>
      <li>Weitere von Ihnen gewünschte Publikationen, die sich nicht in der Liste befinden, bitte am Ende des jeweiligen Unterkapitels unter „Weitere Publikationen zu diesem Unterkapitel“ hochladen. Leitlinien bitte unterhalb der Literaturliste in der Leitlinienbox hochladen. Bitte laden Sie ausschließlich Leitlinien mit relevantem Bezug hoch. Die KI kann auf öffentlich zugängliche Leitlinien zugreifen. Sollten Sie eine neue Leitlinie als Publikation würdigen wollen, ergänzen Sie diese als „weitere Publikation“ im entsprechenden Unterkapitel.</li>
      <li>Wenn Sie die Arbeit unterbrechen, speichern Sie Ihre Auswahl der Publikationen mit den von Ihnen hochgeladenen Dateien unter „Auswahl speichern“. Es wird eine ZIP-Datei erstellt, die Sie in Ihrem Download-Ordner finden (ggf. Download erlauben). Zum Fortsetzen laden Sie die gespeicherte Datei unter „Gespeicherte Arbeitsliste öffnen“ hoch und arbeiten dort weiter.</li>
      <li>Wenn Sie Ihre Bearbeitung abgeschlossen haben, speichern Sie das Ergebnis erneut unter „Auswahl speichern“. Es entsteht wiederum eine ZIP-Datei. Diese schicken Sie je nach Größe per Mail oder meist über einen Cloud-Link (z. B. WeTransfer) an <b>proapp@myluo.io</b>.</li>
    </ol>
  </div>
  <div id="topics"></div>
  <div class="panel">
    <h2>Leitlinien</h2>
    <div class="meta">Aktuelle Leitlinien als PDF hochladen und dem passenden Unterkapitel zuordnen.</div>
    <div class="up" style="margin-top:6px"><label class="filebtn">Leitlinie hochladen<input type="file" id="glFile" accept=".pdf,application/pdf" multiple></label></div>
    <div id="glList"></div>
  </div>
</main>
<div class="picker" id="picker" role="dialog" aria-modal="true">
  <div class="sheet">
    <div class="sheet-head"><span id="pickTitle">Verschieben nach</span><div class="meta" id="pickSub"></div></div>
    <div class="sheet-list" id="pickList"></div>
    <div class="sheet-foot"><button class="btn small" id="pickCancel">Abbrechen</button></div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script type="application/json" id="data">__DATA__</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("data").textContent);
const PDFJS = window.pdfjsLib || null;
if (PDFJS) PDFJS.GlobalWorkerOptions.workerSrc = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
const state = {topics: {}, order: [], pubs: {}, files: {}, guidelines: [], dirty: false, seq: 0};
const $ = (s, el) => (el || document).querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));

function init(){
  $("#hd").textContent = "Auswahlliste " + (DATA.chapter || "");
  document.title = "Auswahlliste " + (DATA.chapter || "Literatur");
  for (const t of DATA.topics){
    state.order.push(t.id);
    state.topics[t.id] = {meta: t, items: t.items.map(i => i.id)};
    for (const i of t.items) state.pubs[i.id] = Object.assign({}, i, {kind: "pub", file: null, check: ""});
  }
  render();
}

function fold(s){ return String(s || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase(); }
function norm(s){ return fold(s).replace(/[^a-z0-9]+/g, ""); }
function matches(text, p){
  const head = text.slice(0, 6000);
  if (p.doi && head.toLowerCase().includes(p.doi.toLowerCase())) return true;
  const nt = norm(head), title = norm(p.title);
  let tm = !!title && nt.includes(title.slice(0, 40));
  if (!tm){
    const words = (fold(p.title).match(/[a-z0-9]+/g) || []).filter(w => w.length >= 4);
    if (words.length >= 4){
      const hw = new Set(fold(head).match(/[a-z0-9]+/g) || []);
      tm = words.filter(w => hw.has(w)).length / words.length >= 0.9;
    }
  }
  const fa = norm(p.first_author);
  return tm && (!fa || nt.includes(fa));
}
async function pdfText(buf, pages){
  if (!PDFJS) return null;
  try{
    const doc = await PDFJS.getDocument({data: new Uint8Array(buf.slice(0))}).promise;
    let out = "";
    for (let n = 1; n <= Math.min(pages, doc.numPages); n++){
      const c = await (await doc.getPage(n)).getTextContent();
      out += c.items.map(x => x.str + (x.hasEOL ? "\n" : " ")).join("") + "\n";
    }
    return out;
  }catch(e){ return null; }
}
function isPdf(buf){ const b = new Uint8Array(buf.slice(0, 1024)); for (let i = 0; i < b.length - 3; i++) if (b[i]===37&&b[i+1]===80&&b[i+2]===68&&b[i+3]===70) return true; return false; }
function firstLine(text){ return ((text || "").split(/\n/).map(s => s.trim()).find(s => s.length > 25) || "").slice(0, 160); }
function safeName(s){ return String(s).replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 80); }

function selectedCount(){ let n = 0; for (const tid of state.order) for (const id of state.topics[tid].items) if (state.pubs[id].checked) n++; return n; }
function updateCounter(){
  const n = selectedCount(), c = $("#counter");
  c.textContent = "Ausgewählt: " + n + " von " + DATA.target + " vorgesehenen Publikationen";
  c.className = "counter" + (n > DATA.target ? " over" : n < DATA.target ? " under" : "");
  for (const tid of state.order){
    const el = document.getElementById("cnt-" + tid);
    if (el){ const items = state.topics[tid].items; el.textContent = items.filter(i => state.pubs[i].checked).length + " von " + items.length + " angehakt"; }
  }
}

function topicLabel(tid){ const m = state.topics[tid].meta; return ((m.number || "") + " " + m.title).trim(); }
// Auswahl eines Unterkapitels als Liste mit vollständigen Titeln (auch auf dem Handy lesbar)
let pickCb = null;
function openPicker(title, sub, cur, cb){
  let html = "", lastSec = null;
  for (const t of state.order){
    const m = state.topics[t].meta;
    if (m.section && m.section !== lastSec) html += '<div class="sheet-sec">' + esc(m.section) + "</div>";
    lastSec = m.section || lastSec;
    html += '<button class="sheet-opt" data-pick="' + esc(t) + '"' + (t === cur ? " disabled" : "") + ">" + esc(topicLabel(t)) + (t === cur ? " (aktuell)" : "") + "</button>";
  }
  $("#pickTitle").textContent = title; $("#pickSub").textContent = sub || "";
  $("#pickList").innerHTML = html; $("#pickList").scrollTop = 0;
  pickCb = cb; $("#picker").classList.add("open");
}
function closePicker(){ $("#picker").classList.remove("open"); pickCb = null; }

function pubHtml(id, tid, idx, n){
  const p = state.pubs[id];
  const tags = [];
  if (p.status) tags.push('<span class="tag' + (p.status === "Reserve" ? "" : " ochre") + '">' + esc(p.status) + "</span>");
  if (p.tier) tags.push('<span class="tag">geplant: ' + esc(p.tier) + "</span>");
  if (p.priority) tags.push('<span class="tag">Priorität ' + esc(p.priority) + "</span>");
  if (p.quality !== "" && p.quality != null) tags.push('<span class="tag">Qualität ' + esc(p.quality) + "</span>");
  const links = [];
  if (p.pmid) links.push('<a href="https://pubmed.ncbi.nlm.nih.gov/' + esc(p.pmid) + '/" target="_blank" rel="noopener">PubMed</a>');
  if (p.doi) links.push('<a href="https://doi.org/' + esc(p.doi) + '" target="_blank" rel="noopener">DOI</a>');
  let st = "";
  if (p.file){
    st = p.check === "passt" ? '<span class="st ok">✓ ' + esc(p.file.name) + " passt</span>"
       : p.check === "bestätigt" ? '<span class="st ok">✓ ' + esc(p.file.name) + " (von Ihnen bestätigt)</span>"
       : p.check === "zusätzlich" ? '<span class="st ok">✓ ' + esc(p.file.name) + " (zusätzliche Publikation)</span>"
       : p.check === "ungeprüft" ? '<span class="st info">' + esc(p.file.name) + " hochgeladen, Prüfung folgt in der App</span>"
       : p.check === "pruefe" ? '<span class="st info">wird geprüft…</span>'
       : '<span class="st bad">⚠ ' + esc(p.file.name) + " scheint nicht zu passen" + (p.found ? ". Gefunden: „" + esc(p.found) + "“" : "") +
         '</span> <button class="btn small warn" data-act="force" data-id="' + esc(id) + '">Trotzdem verwenden</button>';
    if (p.kind === "pub") st += ' <button class="btn small" data-act="rmfile" data-id="' + esc(id) + '">Datei entfernen</button>';
  } else if (p.kind === "pub" && (p.has_fulltext || p.is_oa)){
    st = '<span class="st info">Volltext ' + (p.has_fulltext ? "liegt der App bereits vor" : "frei verfügbar (Open Access)") + ", Upload optional</span>";
  }
  const up = p.kind === "pub" ? '<span class="up">PDF: <label class="filebtn">Datei hochladen<input type="file" accept=".pdf,application/pdf" data-act="upload" data-id="' + esc(id) + '"></label></span>' : "";
  const rm = p.kind === "extra" ? '<button class="btn small" data-act="rmextra" data-id="' + esc(id) + '">Entfernen</button>' : "";
  return '<div class="pub' + (p.checked ? "" : " off") + '" draggable="true" data-id="' + esc(id) + '">' +
    '<span class="handle" title="Ziehen zum Verschieben">⠿</span>' +
    '<input type="checkbox" data-act="check" data-id="' + esc(id) + '"' + (p.checked ? " checked" : "") + ' aria-label="Auswählen">' +
    "<div>" +
      '<div class="t">' + (idx + 1) + ". " + esc(p.title) + "</div>" +
      '<div class="meta">' + esc([p.authors, p.source].filter(Boolean).join(" · ")) + (links.length ? " · " + links.join(" · ") : "") + "</div>" +
      '<div class="meta">' + tags.join("") + (p.design ? " " + esc(p.design) : "") + "</div>" +
      (p.rationale ? '<div class="why">' + esc(p.rationale) + "</div>" : "") +
    "</div>" +
    '<div class="row">' + up + " " + st + "</div>" +
    '<div class="row">' +
      '<button class="btn small" data-act="up" data-id="' + esc(id) + '"' + (idx === 0 ? " disabled" : "") + ' title="nach oben">↑</button>' +
      '<button class="btn small" data-act="down" data-id="' + esc(id) + '"' + (idx === n - 1 ? " disabled" : "") + ' title="nach unten">↓</button>' +
      '<button class="btn small" data-act="movepick" data-id="' + esc(id) + '">Verschieben nach …</button>' + rm +
    "</div></div>";
}

function render(){
  const root = $("#topics"); let html = "", lastSec = null;
  for (const tid of state.order){
    const m = state.topics[tid].meta, items = state.topics[tid].items;
    if (m.section && m.section !== lastSec) html += '<h2 class="section">' + esc(m.section) + "</h2>";
    lastSec = m.section || lastSec;
    html += '<div class="topic"><div class="topic-head"><h3>' + esc(topicLabel(tid)) + '</h3><span class="topic-count" id="cnt-' + esc(tid) + '"></span></div>';
    html += '<div class="list" data-topic="' + esc(tid) + '">' +
      (items.length ? items.map((id, i) => pubHtml(id, tid, i, items.length)).join("") : '<div class="empty">Noch keine Publikation. Hierher ziehen oder unten hochladen.</div>') + "</div>";
    if (m.guidelines && m.guidelines.length)
      html += '<div class="gl">Leitlinien: ' + m.guidelines.map(g => esc(g.title) + (g.society ? " (" + esc(g.society) + (g.year ? ", " + esc(g.year) : "") + ")" : "") + (g.loaded ? " ✓" : " – bitte unten hochladen, falls vorhanden")).join("; ") + "</div>";
    html += '<div class="extra-box">Weitere Publikationen zu diesem Unterkapitel (PDF): <label class="filebtn">Datei hochladen<input type="file" accept=".pdf,application/pdf" multiple data-act="extra" data-topic="' + esc(tid) + '"></label></div></div>';
  }
  root.innerHTML = html;
  renderGl();
  updateCounter();
}

function renderGl(){
  $("#glList").innerHTML = state.guidelines.map((g, i) =>
    '<div class="gl-item"><span class="st ' + (g.pdf ? "ok" : "bad") + '">' + (g.pdf ? "✓ " : "⚠ kein PDF: ") + esc(g.file.name) + "</span>" +
    '<button class="btn small" data-act="glpick" data-i="' + i + '">' + (g.topic ? "Unterkapitel ändern …" : "Unterkapitel wählen …") + "</button>" +
    (g.topic && state.topics[g.topic] ? '<span class="pick-cur">' + esc(topicLabel(g.topic)) + "</span>" : "") +
    '<button class="btn small" data-act="glrm" data-i="' + i + '">Entfernen</button></div>').join("");
}

function where(id){ for (const tid of state.order){ const i = state.topics[tid].items.indexOf(id); if (i >= 0) return [tid, i]; } return [null, -1]; }
function moveTo(id, tid, pos){
  const [src, i] = where(id); if (src === null) return;
  state.topics[src].items.splice(i, 1);
  const list = state.topics[tid].items;
  if (pos == null || pos > list.length) pos = list.length;
  list.splice(pos, 0, id); state.dirty = true; render();
}
function toast(msg){ const t = $("#toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(toast.h); toast.h = setTimeout(() => t.style.display = "none", 4000); }

async function attach(id, file){
  const p = state.pubs[id]; const buf = await file.arrayBuffer();
  if (!isPdf(buf)){ toast("„" + file.name + "“ ist keine PDF-Datei."); return; }
  p.file = {name: file.name, buf: buf}; p.check = "pruefe"; render();
  const text = await pdfText(buf, 2);
  if (text === null) p.check = "ungeprüft";
  else if (!text.trim()){ p.check = "nein"; p.found = "kein lesbarer Text (eingescannt?)"; }
  else { p.check = matches(text, p) ? "passt" : "nein"; p.found = firstLine(text); }
  state.dirty = true; render();
}
async function addExtra(tid, file){
  const buf = await file.arrayBuffer();
  if (!isPdf(buf)){ toast("„" + file.name + "“ ist keine PDF-Datei."); return; }
  const id = "x" + (++state.seq) + "_" + Date.now();
  const text = await pdfText(buf, 1);
  state.pubs[id] = {id: id, kind: "extra", checked: true, title: firstLine(text) || file.name, authors: "", source: "Datei: " + file.name,
                    status: "zusätzlich", file: {name: file.name, buf: buf}, check: "zusätzlich"};
  state.topics[tid].items.push(id); state.dirty = true; render();
}

document.addEventListener("change", async e => {
  const el = e.target, act = el.dataset.act, id = el.dataset.id;
  if (act === "check"){ state.pubs[id].checked = el.checked; state.dirty = true; el.closest(".pub").classList.toggle("off", !el.checked); updateCounter(); }
  else if (act === "upload" && el.files[0]) await attach(id, el.files[0]);
  else if (act === "extra"){ for (const f of Array.from(el.files)) await addExtra(el.dataset.topic, f); }
  else if (el.id === "glFile"){
    for (const f of Array.from(el.files)){ const buf = await f.arrayBuffer(); state.guidelines.push({file: {name: f.name, buf: buf}, topic: "", pdf: isPdf(buf)}); }
    el.value = ""; state.dirty = true; renderGl();
  } else if (el.id === "openFile" && el.files[0]) await openPackage(el.files[0]);
});
document.addEventListener("click", e => {
  const opt = e.target.closest("[data-pick]");
  if (opt){ const cb = pickCb; closePicker(); if (cb) cb(opt.dataset.pick); return; }
  if (e.target.id === "pickCancel" || e.target.id === "picker"){ closePicker(); return; }
  const el = e.target.closest("[data-act]"); if (!el || el.tagName === "SELECT" || el.tagName === "INPUT") return;
  const act = el.dataset.act, id = el.dataset.id;
  if (act === "up" || act === "down"){ const [tid, i] = where(id); moveTo(id, tid, act === "up" ? i - 1 : i + 1); }
  else if (act === "force"){ state.pubs[id].check = "bestätigt"; state.dirty = true; render(); }
  else if (act === "rmfile"){ state.pubs[id].file = null; state.pubs[id].check = ""; state.dirty = true; render(); }
  else if (act === "rmextra"){ const [tid, i] = where(id); state.topics[tid].items.splice(i, 1); delete state.pubs[id]; state.dirty = true; render(); }
  else if (act === "movepick"){ const [tid] = where(id); openPicker("Verschieben nach", state.pubs[id].title, tid, t => moveTo(id, t, null)); }
  else if (act === "glpick"){ const i = +el.dataset.i, g = state.guidelines[i]; openPicker("Leitlinie zuordnen", g.file.name, g.topic, t => { g.topic = t; state.dirty = true; renderGl(); }); }
  else if (act === "glrm"){ state.guidelines.splice(+el.dataset.i, 1); state.dirty = true; renderGl(); }
});

document.addEventListener("keydown", e => { if (e.key === "Escape" && $("#picker").classList.contains("open")) closePicker(); });

// Ziehen und Ablegen
let dragId = null;
document.addEventListener("dragstart", e => { const c = e.target.closest && e.target.closest(".pub"); if (!c) return; dragId = c.dataset.id; c.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; try{ e.dataTransfer.setData("text/plain", dragId); }catch(_){} });
document.addEventListener("dragend", () => { dragId = null; document.querySelectorAll(".dragging,.drop").forEach(x => x.classList.remove("dragging", "drop")); });
document.addEventListener("dragover", e => { const l = e.target.closest && e.target.closest(".list"); if (!l || !dragId) return; e.preventDefault(); document.querySelectorAll(".drop").forEach(x => x !== l && x.classList.remove("drop")); l.classList.add("drop"); });
document.addEventListener("drop", e => {
  const l = e.target.closest && e.target.closest(".list"); if (!l || !dragId) return; e.preventDefault();
  const tid = l.dataset.topic; let pos = 0;
  for (const c of l.querySelectorAll(".pub")){ if (c.dataset.id === dragId) continue; const r = c.getBoundingClientRect(); if (e.clientY > r.top + r.height / 2) pos++; }
  const id = dragId; dragId = null; moveTo(id, tid, pos);
});
window.addEventListener("beforeunload", e => { if (state.dirty){ e.preventDefault(); e.returnValue = ""; } });

// ZIP (ohne Komprimierung, PDFs sind bereits komprimiert)
const CRC = (() => { const t = new Uint32Array(256); for (let n = 0; n < 256; n++){ let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } return t; })();
function crc32(u){ let c = 0xFFFFFFFF; for (let i = 0; i < u.length; i++) c = CRC[(c ^ u[i]) & 0xFF] ^ (c >>> 8); return (c ^ 0xFFFFFFFF) >>> 0; }
function makeZip(entries){
  const enc = new TextEncoder(), parts = [], central = []; let off = 0;
  for (const [name, data] of entries){
    const nb = enc.encode(name), u = data instanceof Uint8Array ? data : new Uint8Array(data), crc = crc32(u);
    const h = new DataView(new ArrayBuffer(30));
    h.setUint32(0, 0x04034b50, true); h.setUint16(4, 20, true); h.setUint16(6, 0x0800, true); h.setUint16(8, 0, true);
    h.setUint32(14, crc, true); h.setUint32(18, u.length, true); h.setUint32(22, u.length, true); h.setUint16(26, nb.length, true);
    parts.push(h.buffer, nb, u);
    const c = new DataView(new ArrayBuffer(46));
    c.setUint32(0, 0x02014b50, true); c.setUint16(4, 20, true); c.setUint16(6, 20, true); c.setUint16(8, 0x0800, true);
    c.setUint32(16, crc, true); c.setUint32(20, u.length, true); c.setUint32(24, u.length, true); c.setUint16(28, nb.length, true); c.setUint32(42, off, true);
    central.push(c.buffer, nb);
    off += 30 + nb.length + u.length;
  }
  let csize = 0; for (const x of central) csize += x.byteLength;
  const e = new DataView(new ArrayBuffer(22));
  e.setUint32(0, 0x06054b50, true); e.setUint16(8, entries.length, true); e.setUint16(10, entries.length, true);
  e.setUint32(12, csize, true); e.setUint32(16, off, true);
  return new Blob(parts.concat(central, [e.buffer]), {type: "application/zip"});
}
function readZip(buf){
  const dv = new DataView(buf), dec = new TextDecoder(), out = {}; let i = buf.byteLength - 22;
  while (i >= 0 && dv.getUint32(i, true) !== 0x06054b50) i--;
  if (i < 0) throw new Error("kein ZIP");
  let p = dv.getUint32(i + 16, true); const n = dv.getUint16(i + 10, true);
  for (let k = 0; k < n; k++){
    const method = dv.getUint16(p + 10, true), size = dv.getUint32(p + 20, true), nl = dv.getUint16(p + 28, true),
          el = dv.getUint16(p + 30, true), cl = dv.getUint16(p + 32, true), lo = dv.getUint32(p + 42, true);
    const name = dec.decode(new Uint8Array(buf, p + 46, nl));
    if (method !== 0) throw new Error("komprimiert");
    const start = lo + 30 + dv.getUint16(lo + 26, true) + dv.getUint16(lo + 28, true);
    out[name] = buf.slice(start, start + size);
    p += 46 + nl + el + cl;
  }
  return out;
}

function save(){
  const missing = [];
  for (const tid of state.order) for (const id of state.topics[tid].items){ const p = state.pubs[id]; if (p.checked && p.file && p.check === "nein") missing.push(p.title); }
  if (missing.length && !confirm(missing.length + " hochgeladene Datei(en) scheinen nicht zur Publikation zu passen und werden nicht mitgespeichert:\n\n" + missing.slice(0, 5).join("\n") + "\n\nTrotzdem speichern?")) return;
  const entries = [], meta = {format: DATA.format, version: DATA.version, project_id: DATA.project_id, chapter: DATA.chapter,
    created: DATA.created, saved_at: new Date().toISOString(), edited_by: $("#editor").value.trim(), target: DATA.target, topics: [], guidelines: []};
  let k = 0;
  for (const tid of state.order){
    const items = [];
    for (const id of state.topics[tid].items){
      const p = state.pubs[id], it = {kind: p.kind, id: p.kind === "pub" ? id : "", checked: !!p.checked, title: p.title};
      if (p.file && p.check !== "nein"){
        const path = "dateien/" + String(++k).padStart(3, "0") + "_" + safeName(p.file.name);
        entries.push([path, p.file.buf]); it.file = path; it.file_name = p.file.name;
        it.check = p.kind === "extra" ? "zusätzlich" : p.check;
      }
      if (p.kind === "extra" && !it.file) continue;
      items.push(it);
    }
    meta.topics.push({id: tid, items: items});
  }
  for (const g of state.guidelines){
    if (!g.pdf) continue;
    const path = "leitlinien/" + String(++k).padStart(3, "0") + "_" + safeName(g.file.name);
    entries.push([path, g.file.buf]); meta.guidelines.push({file: path, file_name: g.file.name, topic_id: g.topic || ""});
  }
  entries.unshift(["auswahl.json", new TextEncoder().encode(JSON.stringify(meta, null, 1))]);
  const blob = makeZip(entries), a = document.createElement("a");
  const slug = (DATA.chapter || "Kapitel").replace(/[^A-Za-z0-9ÄÖÜäöüß]+/g, "_").slice(0, 40);
  a.href = URL.createObjectURL(blob); a.download = "Auswahl_" + slug + "_" + new Date().toISOString().slice(0, 10) + ".zip";
  document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  state.dirty = false;
  toast("Gespeichert: " + selectedCount() + " Publikationen ausgewählt, Dateigröße " + (blob.size / 1048576).toFixed(1) + " MB.");
}

async function openPackage(file){
  let files;
  try{ files = readZip(await file.arrayBuffer()); }catch(e){ toast("Das Paket konnte nicht gelesen werden. Bitte das mit „Auswahl speichern“ erzeugte ZIP verwenden."); return; }
  const metaBuf = files["auswahl.json"]; if (!metaBuf){ toast("Im Paket fehlt „auswahl.json“."); return; }
  const meta = JSON.parse(new TextDecoder().decode(metaBuf));
  if (meta.project_id && DATA.project_id && meta.project_id !== DATA.project_id && !confirm("Diese Datei gehört zu einer anderen Auswahlliste. Trotzdem öffnen?")) return;
  const known = new Set(state.order), placed = new Set();
  const newItems = {}; for (const tid of state.order) newItems[tid] = [];
  for (const t of meta.topics || []){
    if (!known.has(t.id)) continue;
    for (const it of t.items || []){
      let id = it.id;
      if (it.kind === "extra"){
        if (!it.file || !files[it.file]) continue;
        id = "x" + (++state.seq) + "_" + Date.now();
        state.pubs[id] = {id: id, kind: "extra", checked: it.checked, title: it.title || it.file_name, authors: "", source: "Datei: " + it.file_name, status: "zusätzlich", check: "zusätzlich"};
      } else if (!state.pubs[id]) continue;
      const p = state.pubs[id]; p.checked = !!it.checked;
      if (it.file && files[it.file]){ p.file = {name: it.file_name, buf: files[it.file]}; p.check = it.check || "ungeprüft"; }
      newItems[t.id].push(id); placed.add(id);
    }
  }
  for (const tid of state.order) for (const id of state.topics[tid].items) if (!placed.has(id) && state.pubs[id] && state.pubs[id].kind === "pub") newItems[tid].push(id);
  for (const tid of state.order) state.topics[tid].items = newItems[tid];
  state.guidelines = (meta.guidelines || []).filter(g => files[g.file]).map(g => ({file: {name: g.file_name, buf: files[g.file]}, topic: g.topic_id || "", pdf: true}));
  if (meta.edited_by) $("#editor").value = meta.edited_by;
  state.dirty = false; render(); toast("Arbeitsliste geöffnet.");
}

$("#saveBtn").addEventListener("click", save);
$("#openBtn").addEventListener("click", () => $("#openFile").click());
init();
</script>
</body>
</html>
"""
