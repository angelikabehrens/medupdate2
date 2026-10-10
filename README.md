# medupdate 2 – automatisiertes wissenschaftliches Manuskript

Web-App (Python/Streamlit) als Nachfolger von MyLuo. Aus Inhaltsverzeichnis, Formatvorlage und
Hinweisen für Autoren entsteht weitgehend automatisch ein Gesamtmanuskript (Deutsch und Englisch)
mit kritischer Würdigung, State of the Art und formatiertem Literaturverzeichnis.

## Ablauf in zwei Schritten
**Schritt 1:** E-Mail, Inhaltsverzeichnis, Manuskript-Formatvorlage (.docx) und Hinweise für Autoren
hochladen. Die App liest die Themen und Regeln aus, sucht je Thema mit mehreren KI-geplanten
PubMed-Strategien, bewertet die Treffer (Relevanz, Evidenz, Praxisrelevanz, Neuheit, Bias-Risiko) und
schlägt nur Arbeiten der Priorität A vor (plus Reserveliste). Wie viele Arbeiten ein Thema erhält und wie
lang jede Besprechung wird, richtet sich nach der Studienqualität, dann nach der Bedeutung der
Fragestellung für das Themengebiet und nachrangig nach dem Impact Factor des Journals; die
Gesamtlänge laut Hinweisen für Autoren wird dabei eingehalten. Parallel sucht sie aktuelle, frei
verfügbare Leitlinien.
Die Auswahl lässt sich anpassen; die Empfehlungsliste gibt es als Word-Datei.

**Schritt 2:** Originalpublikationen (PDF), eigene Manuskripte der letzten 3 Jahre und optional weitere
Publikationen sowie fehlende Leitlinien hochladen. Danach automatisch: Volltexte (Upload oder Open
Access), kritische Würdigung mit Kommentar und Fazit, Kürzung nach Qualität und Seitenlimit,
Übersetzung ins Deutsche, State of the Art (einer je Hauptabschnitt, vor dem Thema mit den meisten
Publikationen; 3–5 Kernaussagen aus der Einleitung der ersten folgenden Publikation, ohne Volltext per
Websuche), Stilanpassung an die eigenen Manuskripte,
Abbildungsauswahl, Zusammenführung und Prüfung der Literatur über PubMed, Formatierung nach den
Hinweisen für Autoren, Übertragung in die Formatvorlage und Prüfprotokoll.

Zwischenstände lassen sich in der Seitenleiste speichern und später wieder laden.

## Dateien im Repository
- `app.py` – Oberfläche
- `medupdate/` – Bausteine (PubMed, Empfehlung, Leitlinien, Würdigung, Literatur, Word, Formalprüfung)
- `tests/test_pipeline_offline.py` – Test des Gesamtablaufs mit simulierter KI (ohne Internet)
- `requirements.txt`, `secrets_beispiel.toml`, `README.md`

## Bereitstellung (Streamlit Community Cloud)
1. Repository auf GitHub (Hauptdatei: `app.py`).
2. Auf https://share.streamlit.io mit GitHub anmelden, "Create app", Repository/Branch/`app.py` wählen.
3. Unter "Advanced settings" im Feld **Secrets** die Werte einfügen (Format siehe `secrets_beispiel.toml`):
   - `ANTHROPIC_API_KEY` – gemeinsamer KI-Zugang (alternativ gibt jeder Nutzer seinen Schlüssel ein)
   - `APP_PASSWORD` – optionaler Zugangscode
   - `UNPAYWALL_EMAIL` – optional
   - `PARALLEL_WORKERS` – optional, Standard 2; bei Abbrüchen wegen Rate-Limit auf `"1"` setzen
4. Zugriff unter "Share" auf **private** stellen und Nutzer per E-Mail einladen.

**Niemals** einen API-Schlüssel oder Zugangscode in `app.py` oder in eine Datei im Repository schreiben.

## Lokal in VS Code
```
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```
Lokale Secrets in `.streamlit/secrets.toml` (wird per `.gitignore` nicht hochgeladen).
Test ohne Internet: `python tests/test_pipeline_offline.py`

## Hinweise für Nutzer
- Keine Patientendaten oder anderweitig vertrauliche Inhalte hochladen: Texte werden zur Verarbeitung an die
  Anthropic-API gesendet; die App wird von Streamlit in den USA gehostet.
- Es wird nichts dauerhaft auf dem Server gespeichert. Zwischenstand und Word-Dateien immer herunterladen.
- Ein vollständiger Durchlauf dauert je nach Zahl der Publikationen deutlich länger als in MyLuo, weil alle
  Schritte nacheinander automatisch laufen. Die Seite dabei geöffnet lassen.
- Kosten: Die App misst die KI-Kosten (Listenpreise) und zeigt sie nach jedem Lauf an. Für Schritt 2 gibt es
  ein Kostenlimit pro Lauf und einen Probelauf (erstes Thema, höchstens 2 Publikationen). Größter Posten ist
  die kritische Würdigung mit Websuche; der Kontext wird dafür zwischengespeichert (Prompt-Caching).
- Nach einem Abbruch lassen sich alle bis dahin fertigen Würdigungen als Word-Datei herunterladen.
- Neue Anthropic-Konten haben niedrige Rate-Limits (Token pro Minute). Die App wartet dann automatisch;
  bricht sie trotzdem ab, `PARALLEL_WORKERS = "1"` setzen. Ein erneuter Start setzt an derselben Stelle fort.
- Das Ergebnis ist ein Entwurf: Zahlen, Zitate und Literatur vor der Abgabe prüfen (siehe Prüfprotokoll).
