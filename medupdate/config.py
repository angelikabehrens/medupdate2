"""Zentrale Einstellungen: Modelle, Grenzwerte, Zugriff auf die Streamlit-Secrets."""

import streamlit as st

# Modelle an EINER Stelle festlegen, damit ein Wechsel nur hier nötig ist.
# MODEL_MAIN: anspruchsvolle Schritte (kritische Würdigung, State of the Art, Literaturauswahl).
# MODEL_FAST: Massenarbeit mit klarer Vorgabe (Übersetzung, Rechtschreibung, Extraktion).
MODEL_MAIN = "claude-opus-5-5"
MODEL_FAST = "claude-sonnet-5-5"

# Server-Werkzeuge der Claude-API (Websuche / Webseitenabruf)
WEB_SEARCH_TOOL = "web_search_20260209"
WEB_FETCH_TOOL = "web_fetch_20260209"

# Wie viele Publikationen gleichzeitig verarbeitet werden (Rate-Limits des Anthropic-Kontos beachten)
PARALLEL_WORKERS = 3

# Literaturempfehlung: so viele PubMed-Kandidaten pro Thema werden höchstens bewertet
MAX_CANDIDATES_PER_TOPIC = 60
MAX_PUBS_PER_TOPIC = 6  # Obergrenze je Thema; tatsächlich: alle Arbeiten der Priorität A bis zu dieser Zahl


def secret(name: str, default: str = "") -> str:
    """Liest einen Wert aus den Streamlit-Secrets (Cloud: App-Einstellungen, lokal:
    .streamlit/secrets.toml). Liefert den Standardwert, wenn nichts hinterlegt ist."""
    try:
        value = st.secrets.get(name, default)
    except Exception:
        return default
    return str(value) if value else default
