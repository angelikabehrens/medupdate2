"""Schmale Hülle um das offizielle Anthropic-SDK.

Alle KI-Aufrufe der App laufen über diese Klasse. Sie kümmert sich um:
- Streaming (lange Antworten laufen sonst in HTTP-Timeouts),
- Fortsetzung bei stop_reason "pause_turn" (lange Websuchen),
- serverseitigen Rückfall auf ein anderes Modell, falls eine Anfrage abgelehnt wird,
- abgeschnittene Antworten (werden nie stillschweigend weiterverwendet),
- strukturierte JSON-Antworten nach Schema.
"""

from __future__ import annotations

import json

import anthropic

from .config import MODEL_FAST, MODEL_MAIN, WEB_FETCH_TOOL, WEB_SEARCH_TOOL

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class TruncatedResponseError(RuntimeError):
    """Die KI-Antwort wurde wegen des Token-Limits mitten im Text abgebrochen."""


class RefusalError(RuntimeError):
    """Die Anfrage wurde von allen Modellen der Rückfallkette abgelehnt."""


class LLM:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=4, timeout=900.0)

    def _run(self, *, model: str, messages: list, max_tokens: int, effort: str, tools: list | None,
             output_format: dict | None, system: str | None, max_continuations: int = 6):
        output_config: dict = {"effort": effort}
        if output_format:
            output_config["format"] = output_format
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": list(messages),
            "output_config": output_config,
            "betas": [_FALLBACK_BETA],
            "fallbacks": "default",
        }
        if tools:
            kwargs["tools"] = tools
        if system:
            kwargs["system"] = system

        collected: list = []
        message = None
        for _ in range(max_continuations):
            with self.client.beta.messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
            collected.extend(message.content)
            if message.stop_reason != "pause_turn":
                break
            # Lange Recherche wurde unterbrochen (kein Fehler): mit dem bisherigen Verlauf fortsetzen.
            kwargs["messages"] = kwargs["messages"] + [
                {"role": "assistant", "content": [b for b in message.content if b.type != "fallback"]}
            ]

        if message is None:
            raise RuntimeError("Keine Antwort der KI erhalten.")
        if message.stop_reason == "refusal":
            raise RefusalError("Die KI hat die Bearbeitung dieser Anfrage abgelehnt.")
        text = "".join(b.text for b in collected if b.type == "text").strip()
        return text, message.stop_reason

    def text(self, prompt: str | list, *, model: str = MODEL_MAIN, max_tokens: int = 16000,
             effort: str = "high", web_search: int = 0, web_fetch: int = 0, system: str | None = None,
             history: list | None = None, allow_truncated: bool = False) -> str:
        """Einfache Textanfrage. web_search/web_fetch = maximale Anzahl Werkzeugaufrufe (0 = aus)."""
        tools = []
        if web_search:
            tools.append({"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": web_search})
        if web_fetch:
            tools.append({"type": WEB_FETCH_TOOL, "name": "web_fetch", "max_uses": web_fetch})
        messages = list(history or []) + [{"role": "user", "content": prompt}]
        text, stop = self._run(model=model, messages=messages, max_tokens=max_tokens, effort=effort,
                               tools=tools or None, output_format=None, system=system)
        if stop == "max_tokens" and not allow_truncated:
            raise TruncatedResponseError(
                "Die KI-Antwort wurde wegen des Längenlimits abgeschnitten und wird deshalb nicht übernommen."
            )
        return text

    def text_ex(self, prompt: str | list, *, model: str = MODEL_FAST, max_tokens: int = 16000,
                effort: str = "low") -> tuple[str, bool]:
        """Wie text(), gibt aber (Text, abgeschnitten) zurück, statt bei Abbruch einen Fehler auszulösen."""
        text, stop = self._run(model=model, messages=[{"role": "user", "content": prompt}], max_tokens=max_tokens,
                               effort=effort, tools=None, output_format=None, system=None)
        return text, stop == "max_tokens"

    def json(self, prompt: str | list, schema: dict, *, model: str = MODEL_FAST, max_tokens: int = 16000,
             effort: str = "medium", system: str | None = None) -> dict:
        """Antwort als JSON-Objekt, das garantiert dem Schema entspricht (alle Objekte im Schema
        brauchen additionalProperties: false und eine required-Liste)."""
        messages = [{"role": "user", "content": prompt}]
        text, stop = self._run(model=model, messages=messages, max_tokens=max_tokens, effort=effort,
                               tools=None, output_format={"type": "json_schema", "schema": schema},
                               system=system)
        if stop == "max_tokens":
            raise TruncatedResponseError("Die strukturierte KI-Antwort wurde wegen des Längenlimits abgeschnitten.")
        return json.loads(text)


def obj(properties: dict, required: list[str] | None = None) -> dict:
    """Kurzschreibweise für ein JSON-Schema-Objekt im von der API geforderten Format."""
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}
NUM = {"type": "number"}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


def enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


__all__ = ["LLM", "TruncatedResponseError", "RefusalError", "obj", "arr", "enum", "STR", "INT", "BOOL",
           "MODEL_MAIN", "MODEL_FAST"]
