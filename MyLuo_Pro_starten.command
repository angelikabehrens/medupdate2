#!/bin/bash
# MyLuo Pro auf dem eigenen Mac starten (Notfall-Weg, falls Streamlit Cloud gestört ist).
# Doppelklick genügt. Beim ersten Start werden die Programmbibliotheken installiert (einige Minuten).
# Der API-Schlüssel wird NICHT gespeichert: Er wird in der App links im Feld „Anthropic-API-Schlüssel“
# eingegeben (oder Sie legen selbst .streamlit/secrets.toml an; diese Datei wird nie hochgeladen).
cd "$(dirname "$0")" || exit 1
echo "MyLuo Pro wird gestartet …"

if command -v git >/dev/null 2>&1 && [ -d .git ]; then
  echo "Neuester Programmstand wird aus GitHub geholt …"
  git pull --ff-only origin main || echo "Hinweis: Aktualisierung nicht möglich, es wird der vorhandene Stand verwendet."
fi

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo
  echo "Python 3.11 oder neuer ist nicht installiert."
  echo "Bitte von https://www.python.org/downloads/macos/ installieren und danach erneut doppelklicken."
  open "https://www.python.org/downloads/macos/"
  read -r -p "Zum Schließen Enter drücken."
  exit 1
fi

VENV="$HOME/.myluo_pro_venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "Einmalige Einrichtung der Programmbibliotheken …"
  "$PY" -m venv "$VENV" || { read -r -p "Einrichtung fehlgeschlagen. Enter drücken."; exit 1; }
fi
REQ_HASH="$(shasum requirements.txt | cut -d' ' -f1)"
if [ "$(cat "$VENV/.requirements" 2>/dev/null)" != "$REQ_HASH" ]; then
  "$VENV/bin/python" -m pip install -q --upgrade pip
  "$VENV/bin/python" -m pip install -q -r requirements.txt || { read -r -p "Installation fehlgeschlagen. Enter drücken."; exit 1; }
  echo "$REQ_HASH" > "$VENV/.requirements"
fi

echo
echo "Die App öffnet sich gleich im Browser (http://localhost:8501)."
echo "Solange dieses Fenster offen ist, bleibt der Mac wach. Zum Beenden das Fenster schließen."
caffeinate -i -w $$ &
"$VENV/bin/python" -m streamlit run app.py --server.port 8501 --browser.gatherUsageStats false
