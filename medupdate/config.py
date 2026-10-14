"""Zentrale Einstellungen: Modelle, Grenzwerte, Zugriff auf die Streamlit-Secrets."""

import streamlit as st

# Modelle an EINER Stelle festlegen, damit ein Wechsel nur hier nötig ist.
# MODEL_MAIN: anspruchsvolle Schritte (kritische Würdigung, State of the Art, Literaturauswahl).
# MODEL_FAST: Massenarbeit mit klarer Vorgabe (Übersetzung, Rechtschreibung, Extraktion).
# Stand des Programms, in der Seitenleiste angezeigt (bei jeder Änderung hochzählen)
APP_VERSION = "2026-10-14.6"

MODEL_MAIN = "claude-opus-5-5"
MODEL_FAST = "claude-sonnet-5-5"

# Server-Werkzeuge der Claude-API (Websuche / Webseitenabruf)
WEB_SEARCH_TOOL = "web_search_20260209"
WEB_FETCH_TOOL = "web_fetch_20260209"

# Listenpreise in USD pro Million Token (Stand 10/2026, platform.claude.com/docs/en/about-claude/pricing)
# und pro Websuche – nur für die Kostenanzeige in der App.
PRICES = {
    MODEL_MAIN: {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.20},
    MODEL_FAST: {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.10},
}
PRICE_PER_WEB_SEARCH = 0.01

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


# Wie viele Publikationen gleichzeitig verarbeitet werden. Neue Anthropic-Konten haben niedrige
# Rate-Limits (Token pro Minute); bei häufigen Abbrüchen in den Secrets PARALLEL_WORKERS = "1" setzen.
try:
    PARALLEL_WORKERS = max(1, int(secret("PARALLEL_WORKERS", "2")))
except ValueError:
    PARALLEL_WORKERS = 2
