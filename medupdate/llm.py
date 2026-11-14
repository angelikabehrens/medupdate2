"""Schmale Hülle um das offizielle Anthropic-SDK.

Alle KI-Aufrufe der App laufen über diese Klasse. Sie kümmert sich um:
- Streaming (lange Antworten laufen sonst in HTTP-Timeouts),
- Fortsetzung bei stop_reason "pause_turn" (lange Websuchen),
- serverseitigen Rückfall auf ein anderes Modell, falls eine Anfrage abgelehnt wird,
- abgeschnittene Antworten (werden nie stillschweigend weiterverwendet),
- strukturierte JSON-Antworten nach Schema,
- die Kostenaufstellung je Arbeitsschritt (2026-11-13),
- Sammelaufträge über die Message Batches API zum halben Preis („Sparmodus“, 2026-11-13).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

from .config import (BATCH_PRICE_FACTOR, MODEL_FAST, MODEL_MAIN, PRICE_PER_WEB_SEARCH, PRICES, WEB_FETCH_TOOL,
                     WEB_SEARCH_TOOL)

_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Kostenaufstellung: Modul.Funktion des Aufrufers -> Arbeitsschritt, wie ihn die App anzeigt. Ein neuer
# KI-Aufruf ohne Eintrag erscheint unter seinem Funktionsnamen (der Test test_cost_log prüft die Liste).
STEP_APPRAISAL = "Kritische Würdigung"
STEP_APPRAISAL_LEAN = "Kritische Würdigung (schlank, für Kurznachrichten)"
STEP_RANK = "Bewertung der Suchtreffer"
STEP_GUIDELINES = "Leitliniensuche"
STEP_NAMES = {
    "intake.parse_toc": "Inhaltsverzeichnis einlesen",
    "intake.parse_author_rules": "Hinweise für Autoren einlesen",
    "toc_merge.read_book": "Inhaltsverzeichnisse zusammenführen",
    "toc_merge.group_chapters": "Inhaltsverzeichnisse zusammenführen",
    "toc_merge.merge_group": "Inhaltsverzeichnisse zusammenführen",
    "recommend.analyze_chapter": "Suchplan",
    "recommend.plan_search": "Suchplan",
    "recommend.broaden_search": "Suchplan",
    "recommend.rank": STEP_RANK,
    "pipeline.run_safety_net": "Zuordnung neuer Themen",
    "pipeline.run_reroute": "Zuordnung zu passenderen Themen",
    "pipeline.run_followup": "Folgepublikationen großer Studien",
    "pipeline.classify_invasive": "Einstufung interventioneller Studien",
    "guidelines.search_guidelines": STEP_GUIDELINES,
    "guidelines.finish_search": STEP_GUIDELINES,
    "pipeline.run_guideline_needs": "Benötigte Leitlinien bestimmen",
    "pipeline.match_uploads": "Hochgeladene Dateien zuordnen",
    "pipeline.reassign_topics": "Themenzuordnung prüfen",
    "appraisal.generate_appraisal": STEP_APPRAISAL,
    "appraisal.fix_citation_consistency": "Literaturziffern der Würdigung angleichen",
    "appraisal.joint_appraisal": "Gemeinsame Würdigung",
    "pipeline.find_groups": "Gemeinsame Fragestellungen finden",
    "appraisal.shorten": "Kürzung",
    "appraisal.shorten_joint": "Kürzung",
    "appraisal.kurznachricht": "Kurznachricht",
    "appraisal.translate_structured": "Übersetzung",
    "appraisal.translate_abstract": "Übersetzung",
    "appraisal.translate_appraisal": "Übersetzung",
    "appraisal.style_profile": "Stilanpassung",
    "appraisal.apply_style": "Stilanpassung",
    "appraisal.generate_sota_from_intro": "State of the Art",
    "appraisal.generate_sota": "State of the Art",
    "appraisal.translate_sota": "State of the Art",
    "pipeline.vary_fazit_openings": "Fazit-Formulierungen",
    "pipeline.find_own_refs": "Rückbezüge auf frühere Manuskripte",
    "pipeline.topic_comments": "Gemeinsame Kommentare",
    "pipeline.find_transitions": "Überleitungen",
    "pipeline.headings_de": "Überschriften",
    "pipeline.keyword_headings": "Überschriften",
    "references.apply_style": "Literaturverzeichnis formatieren",
    "formal._fix_spelling_batch": "Rechtschreibprüfung",
    "formal.run_formal_check_ai": "Formale Prüfung",
}
OTHER_STEP = "Sonstiges"


def caller_step() -> str:
    """Arbeitsschritt des Aufrufers für die Kostenaufstellung: die erste Funktion außerhalb dieses Moduls
    (bei inneren Hilfsfunktionen die umgebende Funktion)."""
    f = sys._getframe(1)
    while f is not None and f.f_globals.get("__name__") == __name__:
        f = f.f_back
    if f is None:
        return OTHER_STEP
    module = (f.f_globals.get("__name__") or "").rsplit(".", 1)[-1]
    name = getattr(f.f_code, "co_qualname", f.f_code.co_name).split(".<locals>")[0]
    return STEP_NAMES.get(f"{module}.{name}", f"{module}.{name}")


@dataclass
class Job:
    """Ein KI-Auftrag als Daten: lässt sich sofort ausführen (LLM.run) oder mit anderen gesammelt einreichen
    (LLM.run_batch). key ist die dauerhafte Kennung des Auftrags (z.B. die PMID): über sie findet die App
    das Ergebnis eines Sammelauftrags auch nach einem Neustart wieder."""
    key: str
    prompt: str | list
    step: str
    model: str = MODEL_MAIN
    max_tokens: int = 16000
    effort: str = "high"
    web_search: int = 0
    schema: dict | None = None
    system: str | None = None
    est_usd: float = 0.0  # geschätzte Kosten im Sammelauftrag (für das Kostenlimit vor dem Einreichen)


class TruncatedResponseError(RuntimeError):
    """Die KI-Antwort wurde wegen des Token-Limits mitten im Text abgebrochen."""


class RefusalError(RuntimeError):
    """Die Anfrage wurde von allen Modellen der Rückfallkette abgelehnt."""


class AccountError(RuntimeError):
    """Problem mit dem Anthropic-Konto (Guthaben, Ausgabenlimit, Schlüssel) – Wiederholen hilft nicht."""


class BudgetExceeded(AccountError):
    """Das in der App eingestellte Kostenlimit für diesen Lauf ist erreicht."""


class OpenBatchError(AccountError):
    """Bei Anthropic läuft ein Sammelauftrag, der zu diesem Projekt gehören könnte. Der direkte Ablauf hält an,
    bevor er dieselben Aufträge womöglich ein zweites Mal bezahlt."""


# Wartezeiten bei Rate-Limit/Überlastung: lange genug, dass sich das Minutenkontingent erholt
_WAITS = [20, 40, 60, 90, 120, 120, 120]


def _explain(ex: Exception) -> Exception:
    """Übersetzt API-Fehler in verständliche deutsche Meldungen."""
    if isinstance(ex, anthropic.AuthenticationError):
        return AccountError("Der API-Schlüssel ist ungültig oder wurde gelöscht.")
    if isinstance(ex, anthropic.PermissionDeniedError):
        return AccountError("Der API-Schlüssel hat keine Berechtigung für dieses Modell oder diese Funktion "
                            f"(z.B. Websuche in der Console deaktiviert). Details: {ex}")
    if isinstance(ex, anthropic.APIStatusError):
        msg = str(ex).lower()
        billing_words = ("credit balance", "billing", "spend limit", "usage limit")
        if ex.status_code == 402 or (ex.status_code == 400 and any(w in msg for w in billing_words)):
            return AccountError("Das Anthropic-Konto meldet ein Abrechnungsproblem: Guthaben aufgebraucht oder "
                                "monatliches Ausgabenlimit (Console → Settings → Limits) erreicht. "
                                f"Details: {ex}")
        if isinstance(ex, anthropic.RateLimitError):
            return RuntimeError("Rate-Limit des Anthropic-Kontos wiederholt erreicht (zu viele Token pro "
                                "Minute). Das ist kein Guthabenproblem: neue Konten haben niedrige Limits, "
                                "die mit der Zeit steigen. Bitte in den Secrets PARALLEL_WORKERS = \"1\" "
                                f"setzen und erneut starten. Details: {ex}")
    return ex


# ---------- Sammelaufträge (Message Batches API) ----------
BATCH_MAX_ROUNDS = 4       # ein Auftrag läuft höchstens so oft (Fortsetzungen nach „pause_turn“ eingerechnet)
BATCH_KEEP_DAYS = 2        # abgeholte Ergebnisse bleiben so lange als Sicherung bei Anthropic liegen
BATCH_SCAN_DAYS = 7        # so weit zurück wird bei Anthropic nach Sammelaufträgen dieses Projekts gesucht
BATCH_MAX_REQUESTS = 2000  # je Sammelauftrag (Anthropic erlaubt weit mehr; hält die Ergebnisdatei handlich)
STASH_KEEP_DAYS = 30       # abgeholte, in diesem Lauf nicht benötigte Ergebnisse so lange im Projekt aufheben
STASH_MAX = 400            # … und höchstens so viele (die ältesten fallen zuerst weg)
STEP_EARLIER = "Sammelauftrag eines früheren Laufs"
_ROUND_RE = re.compile(r"^(.*)_r(\d+)$")  # Kennung einer Fortsetzung: <Auftrag>_r<Runde>


def _poll_seconds(waited: float) -> int:
    """Abstand der Statusabfragen: anfangs dicht (kleine Sammelaufträge sind in Minuten fertig), dann seltener."""
    return 15 if waited < 180 else (30 if waited < 900 else 60)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds")


def _clock(dt: datetime) -> str:
    """Uhrzeit für Meldungen in deutscher Zeit (die Server laufen in UTC). Fehlen dem Server die
    Zeitzonendaten, steht ausdrücklich UTC dabei."""
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Europe/Berlin")).strftime("%H:%M Uhr")
    except Exception:  # noqa: BLE001
        return dt.astimezone(timezone.utc).strftime("%H:%M Uhr (UTC)")


def _open_batch_error(unclear: dict) -> "OpenBatchError":
    return OpenBatchError(
        f"Bei Anthropic läuft seit {_clock(min(unclear.values()))} ein Sammelauftrag, den die App keinem "
        "Projekt sicher zuordnen kann (eingereicht vor einem Neustart der App, von Ihrem zweiten Server oder "
        "von einem anderen Programm mit demselben KI-Zugang). Gehört er zu diesem Projekt, würde der direkte "
        "Ablauf dieselben Aufträge ein zweites Mal bezahlen. Deshalb hält die App an, bevor sie diese Aufträge "
        "direkt bezahlt. So geht es weiter: Starten Sie den Schritt mit dem Häkchen „Sparmodus“ – die App "
        "wartet dann das Ende des Sammelauftrags ab und übernimmt, was zu diesem Projekt gehört. Oder starten "
        "Sie später noch einmal ohne Sparmodus: Sobald der Sammelauftrag beendet ist (meist nach unter einer "
        "Stunde, spätestens nach 24 Stunden), erkennt die App, zu welchem Projekt er gehört.")


def _when(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _safe_prefix(prefix: str) -> str:
    """Projektkennung für Auftragskennungen (Anthropic erlaubt Buchstaben, Ziffern, „-“ und „_“; der
    Unterstrich bleibt den Fortsetzungen vorbehalten)."""
    return re.sub(r"[^A-Za-z0-9-]", "", prefix or "")[:30] or "projekt"


class BatchNotes:
    """Merkliste der eingereichten Sammelaufträge auf dem Server: {"projects": {Projekt: {Auftrag: Zeit}},
    "seen": {Auftrag: erste Auftragskennung}}. Sie überlebt einen Neustart der App (eigener Server:
    dauerhaft; Streamlit Cloud: solange der Container besteht). So findet die App einen laufenden
    Sammelauftrag nach einem Neustart wieder, statt ihn ein zweites Mal einzureichen und zu bezahlen.
    Enthält nur Auftragsnummern und Kennungen, keine Inhalte."""

    def __init__(self, path: str | Path | None = None):
        if path is None:
            base = os.environ.get("MYLUO_STATE_DIR") or (
                "/opt/myluo/home" if os.path.isdir("/opt/myluo/home") else tempfile.gettempdir())
            path = Path(base) / "myluo_sammelauftraege.json"
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("projects"), dict):
                if not isinstance(data.get("seen"), dict):
                    data["seen"] = {}
                return {"projects": data["projects"], "seen": data["seen"]}
        except (OSError, ValueError):
            pass
        data = {"projects": {}, "seen": {}}
        self._save(data)
        return data

    def _save(self, data: dict) -> None:
        limit = _now() - timedelta(days=31)  # Anthropic löscht Ergebnisse nach 29 Tagen
        for prefix in list(data["projects"]):
            data["projects"][prefix] = {b: t for b, t in data["projects"][prefix].items()
                                        if (_when(t) or _now()) > limit}
            if not data["projects"][prefix]:
                del data["projects"][prefix]
        data["seen"] = dict(list(data.get("seen", {}).items())[-300:])
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # ohne Merkliste bleibt die Suche bei Anthropic (LLM._batch_known)

    def add(self, prefix: str, batch_id: str) -> None:
        with self._lock:
            data = self._load()
            data["projects"].setdefault(prefix, {})[batch_id] = _iso()
            self._save(data)

    def remove(self, prefix: str, batch_id: str) -> None:
        with self._lock:
            data = self._load()
            if data["projects"].get(prefix, {}).pop(batch_id, None) is not None:
                self._save(data)

    def ids(self, prefix: str) -> list[str]:
        return list(self.times(prefix))

    def times(self, prefix: str) -> dict[str, str]:
        """Noch nicht abgeholte Sammelaufträge eines Projekts: {Auftragsnummer: Zeit des Einreichens}."""
        with self._lock:
            return dict(self._load()["projects"].get(prefix, {}))

    def all_ids(self) -> set[str]:
        with self._lock:
            return {b for ids in self._load()["projects"].values() for b in ids}

    def first_id(self, batch_id: str) -> str | None:
        """Erste Auftragskennung eines schon einmal geprüften fremden Sammelauftrags (None = noch nicht geprüft)."""
        with self._lock:
            return self._load()["seen"].get(batch_id)

    def remember(self, batch_id: str, first_id: str) -> None:
        with self._lock:
            data = self._load()
            data["seen"][batch_id] = first_id
            self._save(data)


def _add(total: dict, row: dict) -> None:
    for k, v in row.items():
        total[k] = total.get(k, 0) + v


def _block_dict(block) -> dict:
    """Inhaltsblock einer Antwort als einfaches Wörterbuch (zum Zurückschicken bei einer Fortsetzung)."""
    if isinstance(block, dict):
        return block
    if hasattr(block, "to_dict"):
        return block.to_dict(mode="json")
    return dict(vars(block))


def run_job(llm, job: Job):
    """Führt einen Auftrag sofort aus (voller Preis, Antwort in Minuten). Funktioniert mit jedem Objekt, das
    text() und json() wie LLM anbietet."""
    if job.schema:
        return llm.json(job.prompt, job.schema, model=job.model, max_tokens=job.max_tokens, effort=job.effort,
                        system=job.system, step=job.step)
    return llm.text(job.prompt, model=job.model, max_tokens=job.max_tokens, effort=job.effort,
                    web_search=job.web_search, system=job.system, step=job.step)


class LLM:
    def __init__(self, api_key: str, on_wait=None, budget_usd: float = 0.0):
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=900.0)
        self.budget_usd = budget_usd  # 0 = kein Limit
        self.cost_usd = 0.0
        self.web_searches = 0
        self.stop_requested = False
        self.ledger: dict[str, dict] = {}  # Kostenaufstellung dieses Laufs: Arbeitsschritt -> Zähler
        self.notes = BatchNotes()
        self._sink: dict | None = None
        self._lock = threading.Lock()
        self.on_wait = on_wait or (lambda msg: None)

    def bind(self, proj: dict) -> None:
        """Kosten laufend im Projekt mitschreiben (proj["cost_usd"] und proj["cost_log"]), damit Summe und
        Aufstellung je Arbeitsschritt auch nach einem Abbruch stimmen."""
        self._sink = proj

    def _account(self, model: str, usage, step: str = OTHER_STEP, *, new_call: bool = True,
                 stop: str | None = None, batch: bool = False) -> float:
        p = next((v for k, v in PRICES.items() if (model or "").startswith(k)), PRICES[MODEL_MAIN])
        g = lambda name: getattr(usage, name, 0) or 0  # noqa: E731
        stu = getattr(usage, "server_tool_use", None)
        searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        details = getattr(usage, "output_tokens_details", None)
        thinking = (getattr(details, "thinking_tokens", 0) or 0) if details else 0
        tokens_usd = (g("input_tokens") * p["input"] + g("output_tokens") * p["output"]
                      + g("cache_creation_input_tokens") * p["cache_write"]
                      + g("cache_read_input_tokens") * p["cache_read"]) / 1e6
        fees = searches * PRICE_PER_WEB_SEARCH
        cost = tokens_usd * (BATCH_PRICE_FACTOR if batch else 1.0) + fees
        row = {"calls": 1 if new_call else 0, "requests": 1, "batch_requests": 1 if batch else 0,
               "input": g("input_tokens"), "cache_write": g("cache_creation_input_tokens"),
               "cache_read": g("cache_read_input_tokens"), "output": g("output_tokens"), "thinking": thinking,
               "searches": searches, "usd": cost, "list_usd": tokens_usd + fees,
               "paused": 1 if stop == "pause_turn" else 0, "truncated": 1 if stop == "max_tokens" else 0}
        with self._lock:
            self.cost_usd += cost
            self.web_searches += searches
            _add(self.ledger.setdefault(step, {}), row)
            if self._sink is not None:
                self._sink["cost_usd"] = (self._sink.get("cost_usd") or 0.0) + cost
                _add(self._sink.setdefault("cost_log", {}).setdefault(step, {}), row)
        return cost

    def _check_limits(self) -> None:
        if self.stop_requested:
            raise BudgetExceeded("Der Lauf wurde auf Ihren Wunsch angehalten. Alles bis hierhin Erledigte bleibt "
                                 "erhalten; ein erneuter Start setzt dort fort.")
        if self.budget_usd and self.cost_usd >= self.budget_usd:
            raise BudgetExceeded(
                f"Das eingestellte Kostenlimit von {self.budget_usd:.0f} USD für diesen Lauf ist erreicht "
                f"(≈ {self.cost_usd:.2f} USD verbraucht). Alles bis hierhin Erledigte bleibt erhalten; "
                "ein erneuter Start setzt dort fort.")

    def _stream(self, kwargs: dict):
        """Ein Aufruf mit geduldigem Warten bei Rate-Limit (429) und Überlastung (529/5xx)."""
        for attempt in range(len(_WAITS) + 1):
            try:
                with self.client.beta.messages.stream(**kwargs) as stream:
                    return stream.get_final_message()
            except (anthropic.RateLimitError, anthropic.InternalServerError) as ex:
                if attempt >= len(_WAITS):
                    raise _explain(ex) from ex
                wait = _WAITS[attempt]
                try:
                    wait = max(wait, int(ex.response.headers.get("retry-after", "0")))
                except Exception:
                    pass
                self.on_wait(f"KI-Kontingent ausgelastet – warte {wait} Sek. (Versuch {attempt + 1})")
                time.sleep(wait)
            except anthropic.APIStatusError as ex:
                raise _explain(ex) from ex

    @staticmethod
    def _params(*, model: str, messages: list, max_tokens: int, effort: str, tools: list | None,
                output_format: dict | None, system: str | None) -> dict:
        """Parameter einer Anfrage an die Messages-API, gleich für den direkten Aufruf und den Sammelauftrag."""
        output_config: dict = {"effort": effort}
        if output_format:
            output_config["format"] = output_format
        if tools:
            # Bei Websuche wird der Kontext in jeder Suchrunde erneut verarbeitet: mit Cache-Markierung
            # kosten diese Wiederholungen nur noch einen Bruchteil (Suchergebnisse cacht die API dann selbst).
            messages = [dict(m) for m in messages]
            last = messages[-1]
            if isinstance(last.get("content"), str):
                last["content"] = [{"type": "text", "text": last["content"], "cache_control": {"type": "ephemeral"}}]
        params: dict = {"model": model, "max_tokens": max_tokens, "messages": list(messages),
                        "output_config": output_config}
        if tools:
            params["tools"] = tools
        if system:
            params["system"] = system
        return params

    @staticmethod
    def _tools(web_search: int = 0, web_fetch: int = 0) -> list | None:
        tools = []
        if web_search:
            tools.append({"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": web_search})
        if web_fetch:
            tools.append({"type": WEB_FETCH_TOOL, "name": "web_fetch", "max_uses": web_fetch})
        return tools or None

    def _run(self, *, model: str, messages: list, max_tokens: int, effort: str, tools: list | None,
             output_format: dict | None, system: str | None, step: str = OTHER_STEP, max_continuations: int = 6):
        self._check_limits()
        kwargs = self._params(model=model, messages=messages, max_tokens=max_tokens, effort=effort, tools=tools,
                              output_format=output_format, system=system)
        kwargs["betas"] = [_FALLBACK_BETA]
        kwargs["fallbacks"] = "default"

        collected: list = []
        message = None
        for i in range(max_continuations):
            message = self._stream(kwargs)
            self._account(getattr(message, "model", None) or kwargs["model"], message.usage, step,
                          new_call=i == 0, stop=message.stop_reason)
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
             history: list | None = None, allow_truncated: bool = False, step: str | None = None) -> str:
        """Einfache Textanfrage. web_search/web_fetch = maximale Anzahl Werkzeugaufrufe (0 = aus).
        step: Arbeitsschritt für die Kostenaufstellung (sonst aus dem Aufrufer abgeleitet)."""
        messages = list(history or []) + [{"role": "user", "content": prompt}]
        text, stop = self._run(model=model, messages=messages, max_tokens=max_tokens, effort=effort,
                               tools=self._tools(web_search, web_fetch), output_format=None, system=system,
                               step=step or caller_step())
        if stop == "max_tokens" and not allow_truncated:
            raise TruncatedResponseError(
                "Die KI-Antwort wurde wegen des Längenlimits abgeschnitten und wird deshalb nicht übernommen."
            )
        return text

    def text_ex(self, prompt: str | list, *, model: str = MODEL_FAST, max_tokens: int = 16000,
                effort: str = "low", step: str | None = None) -> tuple[str, bool]:
        """Wie text(), gibt aber (Text, abgeschnitten) zurück, statt bei Abbruch einen Fehler auszulösen."""
        text, stop = self._run(model=model, messages=[{"role": "user", "content": prompt}], max_tokens=max_tokens,
                               effort=effort, tools=None, output_format=None, system=None,
                               step=step or caller_step())
        return text, stop == "max_tokens"

    def json(self, prompt: str | list, schema: dict, *, model: str = MODEL_FAST, max_tokens: int = 16000,
             effort: str = "medium", system: str | None = None, step: str | None = None) -> dict:
        """Antwort als JSON-Objekt, das garantiert dem Schema entspricht (alle Objekte im Schema
        brauchen additionalProperties: false und eine required-Liste)."""
        messages = [{"role": "user", "content": prompt}]
        text, stop = self._run(model=model, messages=messages, max_tokens=max_tokens, effort=effort,
                               tools=None, output_format={"type": "json_schema", "schema": schema},
                               system=system, step=step or caller_step())
        if stop == "max_tokens":
            raise TruncatedResponseError("Die strukturierte KI-Antwort wurde wegen des Längenlimits abgeschnitten.")
        return json.loads(text)

    # ---------- Aufträge als Daten: sofort oder als Sammelauftrag ----------

    def run(self, job: Job):
        """Führt einen Auftrag sofort aus (voller Preis, Antwort in Minuten)."""
        return run_job(self, job)

    def run_summary(self) -> dict:
        """Kosten dieses Laufs je Arbeitsschritt (für das Kostenprotokoll im Projekt)."""
        with self._lock:
            return {step: {"calls": r.get("calls", 0), "usd": round(r.get("usd", 0.0), 4)}
                    for step, r in self.ledger.items() if r.get("usd") or r.get("calls")}

    def _job_params(self, job: Job) -> dict:
        return self._params(
            model=job.model, messages=[{"role": "user", "content": job.prompt}], max_tokens=job.max_tokens,
            effort=job.effort, tools=self._tools(job.web_search),
            output_format={"type": "json_schema", "schema": job.schema} if job.schema else None, system=job.system)

    @staticmethod
    def custom_id(prefix: str, job: Job) -> str:
        """Kennung eines Auftrags im Sammelauftrag: Projekt + Auftrag (nicht der Wortlaut der Anfrage, damit
        ein fertiges Ergebnis auch nach einer Programmänderung noch zugeordnet wird)."""
        return f"{_safe_prefix(prefix)}-{hashlib.md5(job.key.encode('utf-8')).hexdigest()[:24]}"

    def _batch_call(self, fn, *args, **kwargs):
        """Verwaltungsaufruf der Batches-API mit geduldigem Wiederholen bei Netz- oder Lastproblemen."""
        for attempt in range(len(_WAITS) + 1):
            try:
                return fn(*args, **kwargs)
            except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as ex:
                if attempt >= len(_WAITS):
                    raise _explain(ex) from ex
                self.on_wait(f"Verbindung zu Anthropic gestört – warte {_WAITS[attempt]} Sek.")
                time.sleep(_WAITS[attempt])
            except anthropic.APIStatusError as ex:
                raise _explain(ex) from ex

    @staticmethod
    def _gone(ex: Exception) -> bool:
        """Die Ergebnisse sind dauerhaft nicht (mehr) abrufbar – im Unterschied zu Netz- oder Lastproblemen."""
        if isinstance(ex, anthropic.APIStatusError):
            return 400 <= ex.status_code < 500 and ex.status_code not in (401, 403, 408, 409, 429)
        return isinstance(ex, anthropic.AnthropicError) and not isinstance(ex, anthropic.APIConnectionError)

    @staticmethod
    def _close(decoder) -> None:
        close = getattr(decoder, "close", None)
        if close:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    def _batch_first_id(self, batch_id: str) -> str:
        """Kennung des ersten Auftrags eines beendeten Sammelauftrags (zeigt, zu welchem Projekt er gehört).
        Das Ergebnis wird in der Merkliste festgehalten, damit die Datei nicht bei jedem Lauf geöffnet wird."""
        known = self.notes.first_id(batch_id)
        if known is not None:
            return known
        first = ""
        try:
            decoder = self._batch_call(self.client.messages.batches.results, batch_id)
        except Exception as ex:  # noqa: BLE001
            if not self._gone(ex):
                raise
            decoder = None  # keine Ergebnisse (mehr) vorhanden
        if decoder is not None:
            try:
                for item in decoder:
                    first = getattr(item, "custom_id", "") or ""
                    break
            finally:
                self._close(decoder)
        self.notes.remember(batch_id, first)
        return first

    def _batch_wait(self, batch_id: str, log, what: str = "Sammelauftrag", own: bool = True):
        """Wartet, bis der Sammelauftrag bei Anthropic beendet ist; None, wenn es ihn nicht mehr gibt. Wird der
        Lauf angehalten, bricht die App den eigenen Sammelauftrag ab: bereits Fertiges wird danach noch
        abgeholt (es ist bezahlt), noch nicht Bearbeitetes wird nicht berechnet. Einen Sammelauftrag unklarer
        Herkunft (own=False) bricht sie nicht ab, sie beendet nur das Warten."""
        cancelled, last, started = False, "", time.time()
        while True:
            try:
                batch = self._batch_call(self.client.messages.batches.retrieve, batch_id)
            except anthropic.NotFoundError:
                return None  # gelöscht oder abgelaufen
            if batch.processing_status == "ended":
                return batch
            waited = time.time() - started
            if self.stop_requested and not own:
                self._check_limits()
            if self.stop_requested and not cancelled:
                cancelled = True
                try:
                    self._batch_call(self.client.messages.batches.cancel, batch_id)
                except Exception:  # noqa: BLE001 – dann läuft er zu Ende; die Ergebnisse werden abgeholt
                    pass
                log(f"{what} wird auf Ihren Wunsch abgebrochen; bereits Fertiges wird noch abgeholt …")
            c = batch.request_counts
            done = sum(getattr(c, k, 0) or 0 for k in ("succeeded", "errored", "canceled", "expired"))
            total = done + (getattr(c, "processing", 0) or 0)
            if done:
                msg = f"{what}: {done} von {total} Aufträgen fertig."
            else:
                msg = f"{what} wird bei Anthropic bearbeitet ({total} Aufträge)" + (
                    f", seit {int(waited // 600) * 10} Min." if waited >= 600 else ".")
            if msg != last and not cancelled:
                log(msg)
                last = msg
            time.sleep(_poll_seconds(waited))

    def _batch_known(self, prefix: str, state: dict, log, wait_unknown: bool = True,
                     unclear: dict | None = None) -> list[str]:
        """Sammelaufträge dieses Projekts, deren Ergebnisse dieser Projektstand noch nicht übernommen hat: aus
        dem Projekt, aus der Merkliste des Servers und aus der Liste bei Anthropic (dort liegen auch die
        Ergebnisse, falls der Projektstand nach dem Abholen verloren ging). Rückgabe in der Reihenfolge des
        Einreichens (Fortsetzungen bauen auf früheren Ergebnissen auf).

        Ein bei Anthropic noch laufender Sammelauftrag, den weder das Projekt noch die Merkliste dieses Servers
        kennt, ist unklarer Herkunft: Er kann zu diesem Projekt gehören (vor einem Neustart oder vom zweiten
        Server eingereicht) – zuordnen lässt er sich erst nach seinem Ende, an den Auftragskennungen. Er wird
        deshalb abgewartet und dann geprüft, damit nichts doppelt eingereicht wird.

        wait_unknown=False (direkter Ablauf, es wird nur abgeholt): Ein laufender Auftrag unklarer Herkunft
        wird nicht abgewartet, sondern in unclear vermerkt ({Auftragsnummer: Zeitpunkt des Einreichens}); eine
        gestörte Suche bei Anthropic hält den Lauf nicht auf."""
        done = set(state.get("done", {}))
        mine: dict[str, datetime] = {}
        for batch_id, entry in state.get("open", {}).items():
            mine[batch_id] = _when((entry or {}).get("created")) or _now()
        for batch_id, when in self.notes.times(prefix).items():
            mine.setdefault(batch_id, _when(when) or _now())
        mine = {b: t for b, t in mine.items() if b not in done}
        try:
            page = self._batch_call(self.client.messages.batches.list, limit=30)
            listed = list(getattr(page, "data", None) or [])
        except AccountError:
            if wait_unknown:
                raise
            return sorted(mine, key=mine.get)
        except Exception:  # noqa: BLE001 – die Suche ist nur ein Sicherheitsnetz
            return sorted(mine, key=mine.get)
        noted = self.notes.all_ids()
        recent = _now() - timedelta(days=BATCH_SCAN_DAYS)
        for batch in sorted(listed, key=lambda b: _when(b.created_at) or _now()):
            created = _when(batch.created_at) or _now()
            if batch.id in mine:
                mine[batch.id] = created
                continue
            if batch.id in done or batch.id in noted or created < recent:
                continue
            try:
                if batch.processing_status != "ended":
                    if not wait_unknown:  # direkter Ablauf: nicht warten, aber vermerken
                        if unclear is not None:
                            unclear[batch.id] = created
                        continue
                    log(f"Bei Anthropic läuft noch ein Sammelauftrag von {_clock(created)}, der zu diesem Projekt "
                        "gehören könnte. Die App wartet auf sein Ende, damit nichts doppelt bezahlt wird …")
                    if self._batch_wait(batch.id, log, "Früherer Sammelauftrag", own=False) is None:
                        continue
                if self._batch_first_id(batch.id).startswith(prefix + "-"):
                    mine[batch.id] = created
            except AccountError:
                if wait_unknown:
                    raise
                continue
            except Exception:  # noqa: BLE001
                continue
        return sorted(mine, key=mine.get)

    def batch_waiting(self, prefix: str, state: dict | None) -> bool:
        """Liegt für dieses Projekt etwas aus einem Sammelauftrag bereit, das dieser Projektstand noch nicht
        übernommen hat (abgeholte, noch nicht verwendete Ergebnisse oder ein eigener Sammelauftrag bei
        Anthropic)? Wartet nicht. Der direkte Ablauf fragt das, um Bezahltes abzuholen statt neu zu bezahlen.
        Läuft bei Anthropic nur ein Sammelauftrag unklarer Herkunft, hält der Lauf gleich hier an
        (OpenBatchError) – wie run_batch(submit=False), aber noch vor jeder Vorarbeit."""
        state = state or {}
        if state.get("stash"):
            return True
        unclear: dict = {}
        mine = self._batch_known(_safe_prefix(prefix), state, lambda msg: None, wait_unknown=False, unclear=unclear)
        if not mine and self._still_unclear(unclear):
            raise _open_batch_error(unclear)
        return bool(mine)

    def _still_unclear(self, unclear: dict) -> bool:
        """Nachprüfung vor dem Anhalten: Ein Sammelauftrag, den ein anderes Projekt auf diesem Server soeben
        eingereicht hat, steht einen Augenblick später auf der Merkliste und ist dann nicht mehr unklar."""
        if unclear:
            time.sleep(1)
            for batch_id in self.notes.all_ids() & set(unclear):
                del unclear[batch_id]
        return bool(unclear)

    def _batch_cleanup(self, state: dict) -> None:
        """Abgeholte Sammelaufträge nach BATCH_KEEP_DAYS bei Anthropic löschen (bis dahin Sicherung)."""
        limit = _now() - timedelta(days=BATCH_KEEP_DAYS)
        for batch_id, when in list(state.get("done", {}).items()):
            if (_when(when) or _now()) > limit:
                continue
            try:
                self.client.messages.batches.delete(batch_id)
            except Exception:  # noqa: BLE001 – schon gelöscht/abgelaufen: Anthropic löscht nach 29 Tagen selbst
                pass
            state["done"].pop(batch_id, None)

    def run_batch(self, jobs: list[Job], *, prefix: str, state: dict, log=lambda msg: None, submit: bool = True):
        """Führt die Aufträge als Sammelauftrag aus (halber Preis, Ergebnis meist nach unter einer Stunde,
        spätestens nach 24 Stunden) und liefert nach und nach (key, Ergebnis | Exception).

        prefix: Kennung von Projekt und Abschnitt (Teil der Auftragskennung). state: Wörterbuch im Projekt mit
        den Auftragsnummern ({"open", "done", "partial", "stash"}). Zusammen mit der Merkliste des Servers und
        der Liste bei Anthropic sorgt es dafür, dass ein laufender oder fertiger Sammelauftrag nach einem
        Neustart übernommen statt erneut bezahlt wird. Abgeholte Ergebnisse, die dieser Lauf nicht braucht
        (z.B. Probelauf nach einem unterbrochenen Gesamtlauf), bleiben im Projekt liegen ("stash") und werden
        vom nächsten Lauf verwendet. Aufträge ohne Ergebnis (Fehler, abgelaufen, Kostenlimit) bleiben für den
        nächsten Lauf offen. Eine Rückfallkette auf andere Modelle gibt es im Sammelauftrag nicht: lehnt die
        KI einen Auftrag ab, meldet die App das für diesen Auftrag (RefusalError).

        submit=False (direkter Ablauf): Es wird nur abgeholt, was für dieses Projekt schon bezahlt ist – im
        Projekt aufgehobene Ergebnisse und eigene Sammelaufträge bei Anthropic (ein noch laufender wird
        abgewartet). Eingereicht wird nichts; was danach offen bleibt, erledigt der Aufrufer direkt. So kostet
        der Wechsel vom Sparmodus zum direkten Ablauf nichts doppelt."""
        jobs = list(jobs)
        if not jobs:
            return
        prefix = _safe_prefix(prefix)
        by_cid = {self.custom_id(prefix, j): j for j in jobs}
        if len(by_cid) != len(jobs):
            raise ValueError("Zwei Aufträge eines Sammelauftrags haben dieselbe Kennung.")
        open_ids = dict.fromkeys(by_cid)   # noch ohne Ergebnis, in der übergebenen Reihenfolge
        params = {cid: self._job_params(j) for cid, j in by_cid.items()}
        rounds = dict.fromkeys(by_cid, 0)  # so viele Fortsetzungen stecken in params[cid]
        st_open, st_done = state.setdefault("open", {}), state.setdefault("done", {})
        partial, stash = state.setdefault("partial", {}), state.setdefault("stash", {})
        counted: set[str] = set()          # Aufträge, die schon als Aufruf gezählt sind
        held: set[str] = set()             # wegen des Kostenlimits nicht eingereicht

        def finish(job: Job, text: str, stop: str | None):
            if stop == "refusal":
                return RefusalError("Die KI hat die Bearbeitung dieser Anfrage abgelehnt.")
            if stop == "max_tokens":
                return TruncatedResponseError("Die KI-Antwort wurde wegen des Längenlimits abgeschnitten und "
                                              "wird deshalb nicht übernommen.")
            if job.schema:
                try:
                    return json.loads(text)
                except ValueError as ex:
                    return RuntimeError(f"Die strukturierte KI-Antwort ist nicht lesbar: {ex}")
            return text.strip()

        def drain(batch_id: str):
            """Holt die Ergebnisse eines beendeten Sammelauftrags ab. Was schon übernommen ist, steht im
            Projekt (st_open[...]["seen"]) und wird bei einem erneuten Abholen übersprungen."""
            entry = st_open.setdefault(batch_id, {"created": _iso(), "n": 0})
            got = entry.setdefault("seen", [])
            got_set = set(got)

            def mark(full_id: str) -> None:
                if full_id not in got_set:
                    got_set.add(full_id)
                    got.append(full_id)

            decoder = self._batch_call(self.client.messages.batches.results, batch_id)
            try:
                for item in decoder:
                    full_id = getattr(item, "custom_id", "") or ""
                    if full_id in got_set:
                        continue
                    m = _ROUND_RE.match(full_id)
                    cid, rnd = (m.group(1), int(m.group(2))) if m else (full_id, 0)
                    job = by_cid.get(cid) if cid in open_ids else None
                    result = item.result
                    if result.type != "succeeded":  # Fehler, abgelaufen, abgebrochen: nicht berechnet
                        mark(full_id)
                        if job is None:
                            continue
                        partial.pop(cid, None)
                        if result.type == "canceled" and self.stop_requested:
                            continue  # von der Nutzerin angehalten: bleibt ohne Fehlermeldung offen
                        del open_ids[cid]
                        if result.type == "errored":
                            err = getattr(result, "error", None)
                            err = getattr(err, "error", err)
                            yield job.key, RuntimeError(
                                f"Die KI meldet einen Fehler: {getattr(err, 'message', None) or err}")
                        elif result.type == "expired":
                            yield job.key, RuntimeError("Der Sammelauftrag ist nach 24 Stunden abgelaufen, bevor "
                                                        "dieser Auftrag bearbeitet wurde (nicht berechnet).")
                        else:
                            yield job.key, RuntimeError("Der Sammelauftrag wurde abgebrochen, bevor dieser "
                                                        "Auftrag bearbeitet wurde (nicht berechnet).")
                        continue
                    message = result.message
                    stop = message.stop_reason
                    model = getattr(message, "model", None)
                    text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
                    if job is None:
                        # Bezahltes Ergebnis, das dieser Lauf nicht braucht: für einen späteren Lauf aufheben
                        mark(full_id)
                        if cid not in by_cid and cid.startswith(prefix + "-"):
                            self._account(model or MODEL_MAIN, message.usage, STEP_EARLIER, stop=stop, batch=True)
                            if rnd == 0 and stop not in ("pause_turn", "refusal", "max_tokens"):
                                stash[cid] = {"text": text, "t": _iso()}
                                while len(stash) > STASH_MAX:
                                    stash.pop(next(iter(stash)))
                        continue
                    have = partial.get(cid) or {}
                    if rnd and have.get("round") != rnd:
                        # Fortsetzung, deren Anfang in diesem Projektstand fehlt: bezahlt, aber nicht verwertbar
                        mark(full_id)
                        self._account(model or job.model, message.usage, job.step, new_call=False, stop=stop,
                                      batch=True)
                        continue
                    if entry.get("pending") != full_id:  # sonst: beim letzten Abholen schon gezählt
                        self._account(model or job.model, message.usage, job.step, new_call=cid not in counted,
                                      stop=stop, batch=True)
                    counted.add(cid)
                    if rnd:
                        text = have.get("text", "") + text
                    if stop == "pause_turn":
                        # Lange Recherche unterbrochen (kein Fehler): Fortsetzung im nächsten Sammelauftrag
                        mark(full_id)
                        if rounds[cid] != rnd:  # Ergebnis eines früheren Programmlaufs: ohne dessen Verlauf neu
                            params[cid], rounds[cid] = self._job_params(job), 0
                            partial.pop(cid, None)
                        elif rnd + 1 >= BATCH_MAX_ROUNDS:
                            del open_ids[cid]
                            partial.pop(cid, None)
                            yield job.key, RuntimeError("Die Recherche der KI wurde mehrfach unterbrochen und "
                                                        "nicht abgeschlossen.")
                        else:
                            params[cid] = {**params[cid], "messages": params[cid]["messages"] + [
                                {"role": "assistant", "content": [
                                    _block_dict(b) for b in message.content if getattr(b, "type", "") != "fallback"]}]}
                            rounds[cid] = rnd + 1
                            partial[cid] = {"round": rnd + 1, "text": text}
                        continue
                    del open_ids[cid]
                    partial.pop(cid, None)
                    entry["pending"] = full_id
                    yield job.key, finish(job, text, stop)
                    mark(full_id)  # erst jetzt: der Aufrufer hat das Ergebnis übernommen
                    entry.pop("pending", None)
            finally:
                self._close(decoder)

        # 1. Ergebnisse, die ein früherer Lauf schon abgeholt hat
        limit = _now() - timedelta(days=STASH_KEEP_DAYS)
        for cid in [c for c, e in stash.items() if (_when((e or {}).get("t")) or _now()) < limit]:
            stash.pop(cid, None)
        for cid in list(open_ids):
            if cid in stash:
                del open_ids[cid]
                yield by_cid[cid].key, finish(by_cid[cid], stash.pop(cid).get("text", ""), None)
        if not open_ids:
            return

        # 2. Offene Sammelaufträge dieses Projekts abholen, danach für alles Fehlende einen neuen einreichen
        self._batch_cleanup(state)
        unclear: dict[str, datetime] = {}
        known = self._batch_known(prefix, state, log, wait_unknown=submit, unclear=unclear)
        created, max_new = 0, BATCH_MAX_ROUNDS + (len(jobs) - 1) // BATCH_MAX_REQUESTS
        while True:
            if known:
                batch_id = known.pop(0)
                log("Ein früherer Sammelauftrag dieses Projekts wird abgeholt (nichts wird doppelt bezahlt) …")
            else:
                if not submit:
                    break
                todo = sorted((c for c in open_ids if c not in held), key=lambda c: rounds[c] == 0)
                if not todo or created >= max_new or self.stop_requested:
                    break
                self._check_limits()
                todo = todo[:BATCH_MAX_REQUESTS]
                if self.budget_usd:  # nur einreichen, was nach Schätzung ins Kostenlimit passt
                    room, fit = self.budget_usd - self.cost_usd, []
                    for cid in todo:
                        room -= by_cid[cid].est_usd * (0.5 if rounds[cid] else 1.0)
                        if room < 0 and fit and not rounds[cid]:
                            held.add(cid)
                        else:
                            fit.append(cid)
                    todo = fit
                batch = self._batch_call(self.client.messages.batches.create, requests=[
                    {"custom_id": cid if not rounds[cid] else f"{cid}_r{rounds[cid]}", "params": params[cid]}
                    for cid in todo])
                batch_id = batch.id
                created += 1
                st_open[batch_id] = {"created": _iso(), "n": len(todo), "step": by_cid[todo[0]].step}
                self.notes.add(prefix, batch_id)
                if any(rounds[c] for c in todo):
                    log(f"{len(todo)} unterbrochene Recherche(n) werden in einem weiteren Sammelauftrag "
                        "fortgesetzt …")
                else:
                    log(f"Sammelauftrag mit {len(todo)} Aufträgen bei Anthropic eingereicht (halber Preis). Das "
                        "Ergebnis kommt meist innerhalb einer Stunde, spätestens nach 24 Stunden. Sie können das "
                        "Fenster schließen; die App holt das Ergebnis ab.")
            before = len(open_ids)
            if self._batch_wait(batch_id, log) is not None:
                for attempt in range(3):
                    try:
                        yield from drain(batch_id)
                        break
                    except AccountError:
                        raise
                    except Exception as ex:  # noqa: BLE001
                        if self._gone(ex):
                            log("Die Ergebnisse eines früheren Sammelauftrags sind bei Anthropic nicht mehr "
                                "abrufbar; die fehlenden Aufträge werden neu eingereicht.")
                            break
                        if attempt == 2:
                            raise RuntimeError(
                                "Die Ergebnisse des Sammelauftrags konnten nicht vollständig abgeholt werden "
                                f"({type(ex).__name__}: {ex}). Ein erneuter Start holt sie ab; nichts wird "
                                "doppelt bezahlt.") from ex
                        self.on_wait(f"Abholen des Sammelauftrags gestört – neuer Versuch in {_WAITS[attempt]} Sek.")
                        time.sleep(_WAITS[attempt])
                if before != len(open_ids):
                    log(f"Sammelauftrag abgeholt: {before - len(open_ids)} Aufträge erledigt"
                        + (f", {len(open_ids)} noch offen." if open_ids else "."))
            st_open.pop(batch_id, None)
            st_done[batch_id] = _iso()
            self.notes.remove(prefix, batch_id)

        self._check_limits()  # angehalten oder Kostenlimit erreicht: den Lauf wie beim direkten Aufruf beenden
        if not submit:
            for cid in open_ids:  # der Aufrufer erledigt diese Aufträge direkt, ohne den Stand einer Fortsetzung
                partial.pop(cid, None)
            if open_ids and self._still_unclear(unclear):
                raise _open_batch_error(unclear)
            return
        held &= set(open_ids)
        if held:
            raise BudgetExceeded(
                f"Das eingestellte Kostenlimit von {self.budget_usd:.0f} USD reichte nicht für alle Aufträge: "
                f"{len(held)} wurde(n) nicht eingereicht. Alles Erledigte bleibt erhalten; mit einem höheren "
                "Limit setzt ein erneuter Start dort fort.")
        for cid in list(open_ids):
            yield by_cid[cid].key, RuntimeError("Der Sammelauftrag wurde für diesen Auftrag nicht abgeschlossen; "
                                                "ein erneuter Start holt ihn nach.")


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


__all__ = ["LLM", "Job", "run_job", "TruncatedResponseError", "RefusalError", "obj", "arr", "enum", "STR", "INT",
           "BOOL", "MODEL_MAIN", "MODEL_FAST", "STEP_NAMES"]
