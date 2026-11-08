"""Optionaler Schritt 0 (2026-11-08): Inhaltsverzeichnisse mehrerer Jahrgänge eines Handbuchs je Thema
zusammenführen – wie zuvor von Hand für Angio Update 2024–2026.

Ablauf:
1. Je Datei (PDF, EPUB, Word, Text) die Gliederung lesen: EPUB-Navigation, PDF-Lesezeichen und gedrucktes
   Inhaltsverzeichnis am Anfang, nummerierte Überschriften im Text. Eine KI-Abfrage (schnelles Modell) je
   Datei ordnet das zu Kapiteln mit Autor und Unterpunkten.
2. Eine KI-Abfrage ordnet die Kapitel der Jahrgänge einander zu (gleiches Thema).
3. Je Thema mit mehreren Jahrgängen führt eine KI-Abfrage die Gliederungen zusammen: Dopplungen gestrichen,
   Themen, die nur ein- oder zweimal vorkommen, aufgenommen. Hot Topics und Kapitel aus nur einem Jahrgang
   werden unverändert übernommen (ohne KI).
4. Ausgabe: je Thema eine Word-Datei „Inhaltsverzeichnis <Handbuch> <Jahre>_<Thema>_<Autor(en)>“ und ein ZIP.

Alle Zwischenergebnisse werden im Projekt gespeichert; ein erneuter Lauf bezahlt nur Neues."""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
import zipfile
from html.parser import HTMLParser
from posixpath import dirname, join, normpath

import fitz  # PyMuPDF
from docx import Document
from docx.shared import Cm, Pt

from .config import MODEL_FAST, MODEL_MAIN
from .llm import BOOL, INT, LLM, STR, arr, obj

MAX_SOURCE_CHARS = 150_000
FRONT_PAGES = 14          # gedrucktes Inhaltsverzeichnis (mit Autoren) steht meist am Anfang
FRONT_CHARS = 45_000
SKIP_TITLES = re.compile(r"^(literatur|literaturverzeichnis|referenzen|references|interessenkonflikte?|"
                         r"danksagung|abkürzungen|abkürzungsverzeichnis)\b", re.I)
NUM_LINE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,4})\.?\s+([A-ZÄÖÜa-zäöü„\"(].{2,150})$")
REF_HINT = re.compile(r"et al|PMID|\b(19|20)\d\d\s*[;:]|doi|\bvol\b|https?://", re.I)


# ---------- 1. Gliederung aus Dateien ----------

class _NavParser(HTMLParser):
    """EPUB-3-Navigation (nav.xhtml) oder beliebige verschachtelte <ol>/<li>-Listen: Zeilen mit Einrückung."""

    def __init__(self):
        super().__init__()
        self.depth, self.lines, self.buf, self.in_a, self.in_nav = 0, [], "", False, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "nav" and ("toc" in (a.get("epub:type") or "") or a.get("id") == "toc" or not self.in_nav):
            self.in_nav += 1
        elif tag == "ol":
            self.depth += 1
        elif tag == "a":
            self.in_a, self.buf = True, ""

    def handle_endtag(self, tag):
        if tag == "ol":
            self.depth -= 1
        elif tag == "a" and self.in_a:
            self.in_a = False
            t = " ".join(self.buf.split())
            if t:
                self.lines.append("  " * max(self.depth - 1, 0) + t)
        elif tag == "nav" and self.in_nav:
            self.in_nav -= 1

    def handle_data(self, data):
        if self.in_a:
            self.buf += data


def _epub_outline(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        opf = ""
        if "META-INF/container.xml" in names:
            m = re.search(r'full-path="([^"]+)"', z.read("META-INF/container.xml").decode("utf-8", "replace"))
            opf = m.group(1) if m else ""
        nav = ""
        if opf in names:
            o = z.read(opf).decode("utf-8", "replace")
            m = re.search(r'<item[^>]*properties="[^"]*\bnav\b[^"]*"[^>]*>', o)
            href = re.search(r'href="([^"]+)"', m.group(0)).group(1) if m else ""
            if href:
                nav = normpath(join(dirname(opf), href))
        if nav not in names:
            nav = next((n for n in names if re.search(r"(nav|navigation|toc)\.x?html?$", n, re.I)), "")
        lines = []
        if nav:
            p = _NavParser()
            p.feed(z.read(nav).decode("utf-8", "replace"))
            lines = p.lines
        if not lines:
            ncx = next((n for n in names if n.lower().endswith(".ncx")), "")
            if ncx:
                x = z.read(ncx).decode("utf-8", "replace")
                depth = 0
                for tok in re.finditer(r"<navPoint\b|</navPoint>|<text>(.*?)</text>", x, re.S):
                    if tok.group(0).startswith("<navPoint"):
                        depth += 1
                    elif tok.group(0) == "</navPoint>":
                        depth -= 1
                    else:
                        lines.append("  " * max(depth - 1, 0) + " ".join(tok.group(1).split()))
        # Autorinnen und Autoren stehen meist auf den Kapitel-Titelseiten: kurze Textanfänge mitgeben
        heads = []
        for n in names:
            if re.search(r"\.x?html?$", n, re.I) and n != nav:
                txt = html.unescape(re.sub(r"<[^>]+>", " ", z.read(n).decode("utf-8", "replace")))
                txt = re.sub(r"\S+@\S+", "", " ".join(txt.replace("\u200b", " ").split()))[:220]
                if re.search(r"\b(Dr|Prof|PD)\.? ", txt):  # Kapitel-Titelseiten mit Autorennamen
                    heads.append(f"[{n.rsplit('/', 1)[-1]}] {txt}")
        return ("GLIEDERUNG (EPUB-Navigation):\n" + "\n".join(lines)
                + "\n\nANFÄNGE DER EINZELNEN DATEIEN (für Autorennamen):\n" + "\n".join(heads)[:FRONT_CHARS])


REF_AUTHOR = re.compile(r"^[\w'’ÄÖÜäöüß-]+(?: [\w'’-]+){0,2} [A-Z]{1,3},")
CUT_END = re.compile(r"(–|-|,|\b(und|oder|der|die|das|des|den|von|bei|mit|für|im|in|aus|zur|zum|nach|vs\.?))$", re.I)


def _numbered_lines(text: str) -> list[str]:
    """Nummerierte Überschriften im Fließtext (ohne Literaturangaben); über zwei Zeilen umbrochene
    Überschriften werden zusammengefügt."""
    out, seen = [], set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = NUM_LINE.match(line)
        if not m or REF_HINT.search(line) or REF_AUTHOR.match(m.group(2)):
            continue
        title = m.group(2).strip()
        if "." not in m.group(1):  # Kapitelebene: nur Versalien-Überschriften oder kurze Titel ohne Satzzeichen
            letters = [ch for ch in title if ch.isalpha()]
            upper = letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.6
            if not upper and (len(title) > 60 or re.search(r"[,.;:()%\d]", title)):
                continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if nxt and not NUM_LINE.match(nxt) and len(nxt) < 90 and (CUT_END.search(title) or title.isupper()):
            title += " " + nxt
        key = (m.group(1), title[:40].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{m.group(1)} {title}")
    return out


def _pdf_outline(data: bytes) -> str:
    with fitz.open(stream=data, filetype="pdf") as doc:
        toc = doc.get_toc(simple=True)
        front = "\n".join(doc[i].get_text() for i in range(min(FRONT_PAGES, doc.page_count)))[:FRONT_CHARS]
        full = "\n".join(page.get_text() for page in doc)
    parts = []
    if toc:
        parts.append("LESEZEICHEN DER PDF:\n" + "\n".join("  " * (lvl - 1) + t for lvl, t, _ in toc))
    parts.append("ANFANG DES DOKUMENTS (gedrucktes Inhaltsverzeichnis, Autoren):\n" + front)
    parts.append("NUMMERIERTE ÜBERSCHRIFTEN IM TEXT:\n" + "\n".join(_numbered_lines(full)))
    return "\n\n".join(parts)


def _docx_outline(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    lines = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        m = re.match(r"Heading (\d)|Überschrift (\d)", p.style.name or "")
        lvl = int(m.group(1) or m.group(2)) if m else 0
        lines.append(("  " * (lvl - 1) if lvl else "") + t)
    return "\n".join(lines)


def outline_text(filename: str, data: bytes) -> str:
    """Rohgliederung einer Datei als Text für die KI (ohne KI-Kosten)."""
    name = filename.lower()
    if name.endswith(".epub"):
        text = _epub_outline(data)
    elif name.endswith(".pdf"):
        text = _pdf_outline(data)
    elif name.endswith(".docx"):
        text = _docx_outline(data)
    else:
        raw = data.decode("utf-8", errors="replace")
        text = raw if len(raw) <= MAX_SOURCE_CHARS else (
            "ANFANG DES DOKUMENTS:\n" + raw[:FRONT_CHARS] + "\n\nNUMMERIERTE ÜBERSCHRIFTEN IM TEXT:\n"
            + "\n".join(_numbered_lines(raw)))
    return text.replace("\x02", "").replace("\u00ad", "")[:MAX_SOURCE_CHARS]


ITEM = obj({"level": INT, "title": STR})
BOOK_SCHEMA = obj({
    "handbook": STR, "year": STR,
    "chapters": arr(obj({"number": STR, "title": STR, "authors": arr(STR), "hot_topic": BOOL, "items": arr(ITEM)})),
})


def book_prompt(filename: str, text: str) -> str:
    return f"""Aus der folgenden Rohgliederung eines medizinischen Fortbildungshandbuchs (Datei „{filename}“) \
sollst du die Kapitelstruktur herauslesen.

Gib zurück:
- handbook: Name des Handbuchs ohne Jahreszahl und ohne das Wort „Handbuch“ (z.B. „Angio Update“, \
„Gastro Update“).
- year: Jahrgang (vierstellig), aus Dateiname oder Text.
- chapters: NUR die Fachkapitel in der Reihenfolge des Buchs (keine Titelei, kein Impressum, keine \
Infoblätter, Partner, Anmeldung, Autorenverzeichnis, Register). Je Kapitel:
  - number: Kapitelnummer wie im Buch (z.B. „3“), sonst leer
  - title: Kapiteltitel in normaler Groß-/Kleinschreibung (nicht in Versalien), „HT:“ als „Hot Topic: “ \
ausschreiben
  - authors: Nachnamen der Kapitelautorinnen und -autoren, ohne Titel und Vornamen (leer, wenn nicht erkennbar)
  - hot_topic: true, wenn das Kapitel als Hot Topic (oder „HT“) gekennzeichnet ist
  - items: alle Unterpunkte in Reihenfolge, OHNE Nummern; level 1 = x.y, 2 = x.y.z, 3 = x.y.z.w, \
4 = x.y.z.w.v. Ohne „Literatur“, „Interessenkonflikte“, „Danksagung“. Zeilen, die über einen \
Seitenumbruch fortgesetzt sind, zusammenfügen; Kopf- und Fußzeilen, Seitenzahlen und Literaturangaben \
ignorieren. Nichts erfinden, Wortlaut der Überschriften beibehalten.

ROHGLIEDERUNG:
{text}"""


def read_book(llm: LLM, filename: str, data: bytes) -> dict:
    res = llm.json(book_prompt(filename, outline_text(filename, data)), BOOK_SCHEMA, model=MODEL_FAST,
                   max_tokens=48000, effort="low")
    m = re.search(r"(20\d\d)", filename)
    if not re.fullmatch(r"20\d\d", res.get("year", "")) and m:
        res["year"] = m.group(1)
    for c in res.get("chapters", []):
        c["items"] = [{"level": max(1, min(4, int(i.get("level") or 1))), "title": i["title"].strip()}
                      for i in c.get("items", []) if i.get("title", "").strip()
                      and not SKIP_TITLES.match(i["title"].strip())]
    res["name"] = filename
    return res


# ---------- 2. Kapitel zuordnen ----------

GROUP_SCHEMA = obj({"groups": arr(obj({"title": STR, "hot_topic": BOOL, "members": arr(STR)}))})


def group_prompt(listing: str) -> str:
    return f"""Hier sind die Kapitel mehrerer Jahrgänge eines medizinischen Fortbildungshandbuchs, je Zeile \
„ID | Jahr | Titel | Autoren | Hot Topic“. Ordne Kapitel zum selben Fachthema einander zu, auch wenn der \
Titel zwischen den Jahren leicht abweicht (z.B. „PAVK“ und „Periphere arterielle Verschlusskrankheit“).

Regeln:
- Jede ID genau einer Gruppe; höchstens ein Kapitel je Jahrgang pro Gruppe.
- Ein Hot Topic bildet eine eigene Gruppe (hot_topic true); nur dasselbe Hot Topic in mehreren Jahren \
kommt zusammen.
- title: Titel des neuesten Jahrgangs.
- Kapitel ohne Gegenstück in anderen Jahren bilden eine Gruppe mit einem Mitglied.

KAPITEL:
{listing}"""


def group_chapters(llm: LLM, books: list[dict]) -> list[dict]:
    chapters = {}
    for b_i, b in enumerate(books):
        for c_i, c in enumerate(b.get("chapters", [])):
            chapters[f"B{b_i}C{c_i}"] = (b, c)
    listing = "\n".join(f"{cid} | {b.get('year', '')} | {c['title']} | {', '.join(c.get('authors', []))} | "
                        f"{'ja' if c.get('hot_topic') else 'nein'}" for cid, (b, c) in chapters.items())
    res = llm.json(group_prompt(listing), GROUP_SCHEMA, model=MODEL_FAST, max_tokens=8000, effort="low")
    groups, used = [], set()
    for g in res.get("groups", []):
        members = [m for m in g.get("members", []) if m in chapters and m not in used]
        if members:
            used.update(members)
            groups.append({"title": g.get("title") or chapters[members[-1]][1]["title"],
                           "hot_topic": bool(g.get("hot_topic")), "members": members})
    for cid in chapters:  # nichts verlieren
        if cid not in used:
            groups.append({"title": chapters[cid][1]["title"], "hot_topic": bool(chapters[cid][1].get("hot_topic")),
                           "members": [cid]})
    return groups


# ---------- 3. Zusammenführen ----------

MERGE_SCHEMA = obj({"items": arr(ITEM)})


def _indented(items: list[dict]) -> str:
    return "\n".join("  " * (i["level"] - 1) + i["title"] for i in items)


def merge_prompt(title: str, versions: list[tuple[str, list[dict]]]) -> str:
    blocks = "\n\n".join(f"JAHRGANG {y}:\n{_indented(items)}" for y, items in versions)
    return f"""Führe die Gliederungen des Kapitels „{title}“ aus mehreren Jahrgängen eines medizinischen \
Fortbildungshandbuchs zu EINER Gliederung zusammen. Sie dient als Inhaltsverzeichnis für die Literatursuche \
des nächsten Jahrgangs; es darf kein Thema verloren gehen.

Regeln:
- Die Gliederung des neuesten Jahrgangs ist der Rahmen (Hauptabschnitte und Reihenfolge).
- Dopplungen streichen: Unterpunkte mit gleichem Inhalt, aber anderem Wortlaut nur einmal aufnehmen, im \
Wortlaut des neuesten Jahrgangs (einen älteren nur, wenn er genauer ist).
- Themen, die nur in einem oder zwei Jahrgängen vorkommen, ebenfalls aufnehmen und dem passenden \
Hauptabschnitt zuordnen; nur wenn keiner passt, einen eigenen Abschnitt anlegen.
- Nichts erfinden, nichts zusammenfassen, was inhaltlich verschieden ist. Keine Nummern, keine Jahreszahlen, \
kein „Literatur“, „Interessenkonflikte“, „Einleitung“ oder „Fazit“.
- level 1 = Hauptabschnitt (x.y), 2 = Unterpunkt (x.y.z), 3 und 4 nur, wenn die Vorlagen so tief gegliedert sind.

{blocks}"""


def merge_group(llm: LLM, title: str, versions: list[tuple[str, list[dict]]]) -> list[dict]:
    res = llm.json(merge_prompt(title, versions), MERGE_SCHEMA, model=MODEL_MAIN, max_tokens=16000,
                   effort="medium")
    return [{"level": max(1, min(4, int(i.get("level") or 1))), "title": i["title"].strip()}
            for i in res.get("items", []) if i.get("title", "").strip()]


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:16]


def estimate_usd(n_files: int) -> tuple[float, float]:
    """Grobe Schätzung (Listenpreise): je Handbuch Lesen ≈0,15–0,35 USD, Zusammenführen ≈0,05 USD je Kapitel."""
    return round(0.25 * n_files + 0.1, 1), round(0.6 * n_files + 0.3, 1)


def run_toc_merge(llm: LLM, proj: dict, files: list[tuple[str, bytes]], handbook: str, log) -> None:
    st = proj.setdefault("toc_merge", {"books": {}, "merged": {}})
    keys = []
    for name, data in files:
        k = _sha(data)
        keys.append(k)
        if k not in st["books"]:
            log(f"Inhaltsverzeichnis aus „{name}“ wird gelesen…")
            st["books"][k] = read_book(llm, name, data)
    books = sorted((st["books"][k] for k in dict.fromkeys(keys)), key=lambda b: b.get("year", ""))
    gkey = json.dumps([b["name"] + b.get("year", "") for b in books])
    if st.get("groups_key") != gkey:
        log("Kapitel der Jahrgänge werden einander zugeordnet…")
        st["groups"], st["groups_key"] = group_chapters(llm, books), gkey
    by_id = {f"B{b_i}C{c_i}": (b, c) for b_i, b in enumerate(books) for c_i, c in enumerate(b.get("chapters", []))}
    newest = books[-1].get("year", "") if books else ""
    chapters = []
    for g in st["groups"]:
        mem = sorted((by_id[m] for m in g["members"] if m in by_id), key=lambda bc: bc[0].get("year", ""))
        if not mem:
            continue
        years = [b.get("year", "") for b, _ in mem]
        authors = list(dict.fromkeys(a for _, c in mem for a in c.get("authors", [])))
        latest_b, latest_c = mem[-1]
        if g.get("hot_topic") or len(mem) == 1:
            items, how = latest_c["items"], "unverändert übernommen"
        else:
            mkey = json.dumps([[b.get("year"), c["title"], c["items"]] for b, c in mem], ensure_ascii=False)
            mkey = hashlib.sha1(mkey.encode()).hexdigest()
            if mkey not in st["merged"]:
                log(f"„{g['title']}“ wird zusammengeführt ({', '.join(years)})…")
                st["merged"][mkey] = merge_group(llm, g["title"], [(b.get("year", ""), c["items"]) for b, c in mem])
            items, how = st["merged"][mkey], "zusammengeführt"
        number = latest_c.get("number", "") if latest_b.get("year") == newest else ""
        chapters.append({"title": latest_c["title"] if latest_b.get("year") == newest else g["title"],
                         "years": years, "authors": authors, "items": items, "number": number, "how": how,
                         "hot_topic": bool(g.get("hot_topic"))})
    # Nummern: wie im neuesten Jahrgang, Kapitel ohne aktuelle Nummer dahinter
    used = [int(c["number"]) for c in chapters if c["number"].isdigit()]
    nxt = max(used, default=0)
    newest_order = {c["title"]: i for i, c in enumerate((books[-1].get("chapters", []) if books else []))}
    chapters.sort(key=lambda c: (0, int(c["number"])) if c["number"].isdigit()
                  else (1, newest_order.get(c["title"], 10 ** 6)))
    for c in chapters:
        if not c["number"].isdigit():
            nxt += 1
            c["number"] = str(nxt)
    st["handbook"] = handbook.strip() or (books[-1].get("handbook", "") if books else "") or "Handbuch"
    st["chapters"] = chapters
    st["files"] = [b["name"] for b in books]
    log(f"{len(chapters)} Themen-Inhaltsverzeichnisse erstellt.")


# ---------- 4. Ausgabe ----------

def numbered(chapter: dict) -> list[tuple[int, str, str]]:
    cnt, out = [0] * 6, []
    for i in chapter["items"]:
        d = i["level"]
        cnt[d - 1] += 1
        for k in range(d, 6):
            cnt[k] = 0
        out.append((d, chapter["number"] + "." + ".".join(str(x) for x in cnt[:d]), i["title"]))
    return out


def as_text(chapter: dict) -> str:
    """Als Inhaltsverzeichnis für Schritt 1."""
    return "\n".join([f"{chapter['number']} {chapter['title']}"]
                     + ["  " * (d - 1) + f"{n} {t}" for d, n, t in numbered(chapter)])


def filename(chapter: dict, handbook: str) -> str:
    y = chapter["years"]
    yrs = y[0] if len(set(y)) == 1 else f"{y[0]}_{y[-1]}"
    t = chapter["title"].replace("Hot Topic: ", "Hot Topic ").replace(": ", " – ")
    name = f"Inhaltsverzeichnis {handbook} {yrs}_{t}" + (f"_{'_'.join(chapter['authors'])}" if chapter["authors"] else "")
    return re.sub(r'[\\/:*?"<>|]+', "-", name)[:180] + ".docx"


def chapter_docx(chapter: dict) -> bytes:
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(11)
    doc.add_heading(f"{chapter['number']} {chapter['title']}", level=1)
    for d, n, t in numbered(chapter):
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Cm(0.8 * (d - 1))
        p.paragraph_format.space_after = Pt(2)
        r = p.add_run(f"{n} {t}")
        r.bold = d == 1
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def zip_all(state: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for c in state.get("chapters", []):
            z.writestr(filename(c, state.get("handbook", "")), chapter_docx(c))
    return buf.getvalue()
