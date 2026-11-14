"""Offline-Tests der Sparmaßnahmen (2026-11-13): Kostenaufstellung je Arbeitsschritt, Sammelaufträge über die
Message Batches API, schlanke Würdigung für Kurznachrichten und Merkfunktionen für das erneute Erstellen.
Anthropic, PubMed und Downloads werden simuliert; es entstehen keine Kosten.
Aufruf: python tests/test_savings_offline.py  (oder python -m pytest tests/)"""

import ast
import copy
import io
import json
import re
import sys
import tempfile
import types
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_pipeline_offline as T  # noqa: E402 – gemeinsame Attrappen (FakeLLM, Beispieldatensätze)

from docx import Document  # noqa: E402

from medupdate import appraisal as A, formal, guidelines, pipeline, prompts, pubmed, recommend, references  # noqa: E402
from medupdate import llm as L  # noqa: E402

L.time.sleep = lambda s: None  # Wartezeiten der Sammelaufträge entfallen im Test


# ----------------------------------------------------------------------
# Attrappe der Anthropic-API (direkte Aufrufe und Sammelaufträge)
# ----------------------------------------------------------------------

def _usage(inp=1000, out=500, searches=0):
    return types.SimpleNamespace(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0,
                                 cache_read_input_tokens=0,
                                 server_tool_use=types.SimpleNamespace(web_search_requests=searches))


def _prompt_of(params: dict) -> str:
    content = params["messages"][0]["content"]
    return content if isinstance(content, str) else "".join(b.get("text", "") for b in content)


class FakeServer:
    """Hält die Sammelaufträge wie Anthropic: überlebt den „Neustart“ der App (neues LLM-Objekt)."""

    def __init__(self, answer=None, polls=1):
        self.batches = {}      # id -> {"requests", "status", "created_at", "polls", "results"}
        self.answer = answer or (lambda params, custom_id: ("ok", "end_turn"))
        self.polls = polls     # so viele Statusabfragen bleibt ein Sammelauftrag in Bearbeitung
        self.created = self.cancelled = self.deleted = self.live = 0
        self.fail_running = None  # Ausnahme, solange ein Sammelauftrag noch läuft („Absturz“ beim Warten)

    def client(self):
        srv = self

        class Stream:
            def __init__(self, kw):
                self.kw = kw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                srv.live += 1
                text, stop = srv.answer(self.kw, "")
                return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)],
                                             stop_reason=stop, usage=_usage(), model=self.kw["model"])

        class Batches:
            def create(self, requests):
                srv.created += 1
                bid = f"msgbatch_{srv.created:03d}"  # fortlaufend, auch nach dem Löschen eines Sammelauftrags
                for r in requests:
                    assert not {"stream", "fallbacks", "betas", "speed"} & set(r["params"]), "im Sammelauftrag unzulässig"
                    assert 1 <= len(r["custom_id"]) <= 64
                srv.batches[bid] = {"requests": list(requests), "status": "in_progress", "polls": srv.polls,
                                    "created_at": datetime.now(timezone.utc), "results": None, "cancel": False}
                return types.SimpleNamespace(id=bid)

            def retrieve(self, bid):
                b = srv.batches.get(bid)
                if srv.fail_running and b is not None and b["status"] != "ended":
                    raise srv.fail_running
                if b is None:
                    import anthropic
                    import httpx2 as httpx
                    req = httpx.Request("GET", "https://api.anthropic.com/v1/messages/batches/x")
                    raise anthropic.NotFoundError("not found", response=httpx.Response(404, request=req), body=None)
                if b["status"] != "ended":
                    b["polls"] -= 1
                    if b["polls"] < 0 or b["cancel"]:
                        srv._finish(bid)
                n = len(b["requests"])
                counts = types.SimpleNamespace(processing=0 if b["status"] == "ended" else n,
                                               succeeded=n if b["status"] == "ended" else 0, errored=0, canceled=0,
                                               expired=0)
                return types.SimpleNamespace(id=bid, processing_status=b["status"], request_counts=counts,
                                             created_at=b["created_at"])

            def results(self, bid):
                b = srv.batches[bid]
                assert b["status"] == "ended"
                return iter(b["results"])

            def cancel(self, bid):
                srv.cancelled += 1
                srv.batches[bid]["cancel"] = True

            def delete(self, bid):
                srv.deleted += 1
                srv.batches.pop(bid, None)

            def list(self, limit=20):
                data = [types.SimpleNamespace(id=bid, processing_status=b["status"], created_at=b["created_at"])
                        for bid, b in reversed(list(srv.batches.items()))][:limit]
                return types.SimpleNamespace(data=data)

        return types.SimpleNamespace(
            beta=types.SimpleNamespace(messages=types.SimpleNamespace(stream=lambda **kw: Stream(kw))),
            messages=types.SimpleNamespace(batches=Batches()))

    def _finish(self, bid):
        b = self.batches[bid]
        out = []
        for i, r in enumerate(b["requests"]):
            if b["cancel"] and i > 0:  # der erste Auftrag war schon in Arbeit, der Rest wird abgebrochen
                out.append(types.SimpleNamespace(custom_id=r["custom_id"], result=types.SimpleNamespace(type="canceled")))
                continue
            res = self.answer(r["params"], r["custom_id"])
            if isinstance(res, str):  # "errored" / "expired"
                err = types.SimpleNamespace(error=types.SimpleNamespace(message="kaputt"))
                out.append(types.SimpleNamespace(custom_id=r["custom_id"],
                                                 result=types.SimpleNamespace(type=res, error=err)))
                continue
            text, stop = res
            content = [types.SimpleNamespace(type="text", text=text)]
            if stop == "pause_turn":
                content.insert(0, types.SimpleNamespace(type="server_tool_use", id="srvtoolu_1", name="web_search",
                                                        input={"query": "x"}))
            msg = types.SimpleNamespace(content=content, stop_reason=stop, usage=_usage(searches=1 if "tools" in r["params"] else 0),
                                        model=r["params"]["model"])
            out.append(types.SimpleNamespace(custom_id=r["custom_id"],
                                             result=types.SimpleNamespace(type="succeeded", message=msg)))
        b["results"], b["status"] = out, "ended"


def _llm(server: FakeServer, notes: Path | None = None, budget: float = 0.0) -> L.LLM:
    llm = L.LLM("x", budget_usd=budget)
    llm.client = server.client()
    llm.notes = L.BatchNotes(notes or Path(tempfile.mkdtemp()) / "merkliste.json")
    return llm


def _jobs(*keys, **kw):
    return [L.Job(key=k, prompt=f"Auftrag {k}", step="Testschritt", **kw) for k in keys]


def _collect(gen):
    out = {}
    for key, res in gen:
        out[key] = res
    return out


# ----------------------------------------------------------------------
# Sammelaufträge
# ----------------------------------------------------------------------

def test_batch_basic_and_cost():
    srv = FakeServer(lambda p, cid: ('{"a": 1}' if "output_config" in p and "format" in p["output_config"]
                                     else f"Antwort auf {_prompt_of(p)}", "end_turn"))
    llm = _llm(srv)
    proj = {}
    llm.bind(proj)
    state, log = {}, []
    jobs = _jobs("p1", "p2", web_search=4) + [L.Job(key="j1", prompt="x", step="Schema", schema=L.obj({"a": L.INT}))]
    res = _collect(llm.run_batch(jobs, prefix="abc123-w", state=state, log=log.append))
    assert res == {"p1": "Antwort auf Auftrag p1", "p2": "Antwort auf Auftrag p2", "j1": {"a": 1}}, res
    assert srv.created == 1 and not state["open"] and len(state["done"]) == 1
    req = srv.batches["msgbatch_001"]["requests"][0]
    assert req["params"]["tools"][0]["max_uses"] == 4 and req["params"]["output_config"] == {"effort": "high"}
    assert req["params"]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # halber Preis auf die Token, die Gebühr je Websuche bleibt: (1000*4 + 500*20)/1e6 = 0,014 → 0,007 + 0,01
    row = llm.ledger["Testschritt"]
    assert row["calls"] == 2 and row["batch_requests"] == 2 and row["searches"] == 2
    assert abs(row["usd"] - 2 * (0.007 + 0.01)) < 1e-9 and abs(row["list_usd"] - 2 * (0.014 + 0.01)) < 1e-9
    assert abs(proj["cost_usd"] - llm.cost_usd) < 1e-9 and proj["cost_log"]["Schema"]["calls"] == 1
    assert not llm.notes.ids("abc123-w"), "abgeholte Sammelaufträge stehen nicht mehr auf der Merkliste"
    assert any("halber Preis" in m for m in log)
    # Ein erneuter Lauf mit denselben Aufträgen reicht neu ein (die Ergebnisse hat der Aufrufer ja gespeichert) …
    assert not list(llm.run_batch([], prefix="abc123-w", state=state))
    # … und löscht den abgeholten Sammelauftrag erst nach der Sicherungsfrist bei Anthropic
    state["done"]["msgbatch_001"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    _collect(llm.run_batch(_jobs("p9"), prefix="abc123-w", state=state))
    assert srv.deleted == 1 and "msgbatch_001" not in state["done"]


def test_batch_pause_and_errors():
    def answer(params, cid):
        prompt = _prompt_of(params)
        if "pause" in prompt:
            if cid.endswith("_r1"):
                assert params["messages"][1]["role"] == "assistant"
                assert params["messages"][1]["content"][0]["type"] == "server_tool_use"
                return "Teil zwei.", "end_turn"
            return "Teil eins. ", "pause_turn"
        if "immer" in prompt:
            return "x", "pause_turn"
        if "fehler" in prompt:
            return "errored"
        if "alt" in prompt:
            return "expired"
        if "nein" in prompt:
            return "", "refusal"
        if "lang" in prompt:
            return "abgeschn", "max_tokens"
        return "gut", "end_turn"

    srv = FakeServer(answer)
    llm = _llm(srv)
    state = {}
    res = _collect(llm.run_batch(_jobs("pause", "fehler", "alt", "nein", "lang", "ok", "immer"), prefix="p-w",
                                 state=state))
    assert res["pause"] == "Teil eins. Teil zwei." and res["ok"] == "gut"
    assert isinstance(res["fehler"], RuntimeError) and "kaputt" in str(res["fehler"])
    assert "abgelaufen" in str(res["alt"]) and isinstance(res["nein"], L.RefusalError)
    assert isinstance(res["lang"], L.TruncatedResponseError)
    assert "mehrfach unterbrochen" in str(res["immer"])
    assert srv.created == L.BATCH_MAX_ROUNDS and not state["partial"] and not state["open"]
    row = llm.ledger["Testschritt"]
    # berechnet: pause (2 Anfragen), nein, lang, ok, immer (4 Anfragen); Fehler und Abgelaufenes nicht
    assert row["calls"] == 5 and row["requests"] == 9 and row["paused"] == 5 and row["truncated"] == 1, row


def test_batch_budget_and_stop():
    srv = FakeServer()
    llm = _llm(srv, budget=1.0)
    state, got = {}, {}
    try:
        for k, v in llm.run_batch(_jobs("a", "b", "c", est_usd=0.6), prefix="p-w", state=state):
            got[k] = v
        raise AssertionError("Kostenlimit muss gemeldet werden")
    except L.BudgetExceeded as ex:
        assert "2 wurde(n) nicht eingereicht" in str(ex), ex
    assert got == {"a": "ok"} and len(srv.batches["msgbatch_001"]["requests"]) == 1

    # Anhalten: der Sammelauftrag wird abgebrochen, Fertiges abgeholt, der Rest bleibt ohne Fehlermeldung offen
    srv = FakeServer(polls=3)
    llm = _llm(srv)
    state, got = {}, {}

    def log(msg):
        if "bearbeitet" in msg:
            llm.stop_requested = True

    try:
        for k, v in llm.run_batch(_jobs("a", "b", "c"), prefix="p-w", state=state, log=log):
            got[k] = v
        raise AssertionError("Anhalten muss gemeldet werden")
    except L.BudgetExceeded as ex:
        assert "angehalten" in str(ex)
    assert srv.cancelled == 1 and got == {"a": "ok"} and srv.created == 1, (got, srv.created)


def test_batch_recovery():
    notes = Path(tempfile.mkdtemp()) / "merkliste.json"
    srv = FakeServer(lambda p, cid: (f"Antwort {_prompt_of(p)[-1]}", "end_turn"))
    abc = {"a": "Antwort a", "b": "Antwort b", "c": "Antwort c"}

    def interrupted(state, notes_path, keys):
        """Reicht ein und „stürzt“ beim Warten auf den neuen Sammelauftrag ab."""
        llm = _llm(srv, notes_path)
        srv.fail_running = KeyboardInterrupt()
        try:
            _collect(llm.run_batch(_jobs(*keys), prefix="proj1-w", state=state))
            raise AssertionError("Abbruch erwartet")
        except KeyboardInterrupt:
            pass
        finally:
            srv.fail_running = None

    # 1. Der Projektstand ist erhalten (z.B. Netzfehler): derselbe Sammelauftrag wird abgeholt
    state = {}
    interrupted(state, notes, "abc")
    assert srv.created == 1 and list(state["open"]) == ["msgbatch_001"]
    llm = _llm(srv, notes)
    assert _collect(llm.run_batch(_jobs("a", "b", "c"), prefix="proj1-w", state=state)) == abc
    assert srv.created == 1, "nichts wird ein zweites Mal eingereicht"

    # 2. Der Projektstand ging NACH dem Abholen verloren (älterer Zwischenstand geladen): die Ergebnisse liegen
    # noch als Sicherung bei Anthropic und kommen ohne neue Kosten zurück
    llm = _llm(srv, notes)
    state = {}
    assert _collect(llm.run_batch(_jobs("a", "b", "c"), prefix="proj1-w", state=state)) == abc
    assert srv.created == 1 and "msgbatch_001" in state["done"]

    # 3. Der Projektstand ging WÄHREND des Sammelauftrags verloren; die Merkliste des Servers kennt ihn
    done = {"msgbatch_001": L._iso()}
    interrupted({"done": dict(done)}, notes, "def")
    assert srv.created == 2
    llm = _llm(srv, notes)
    state = {"done": dict(done)}
    assert len(_collect(llm.run_batch(_jobs("d", "e", "f"), prefix="proj1-w", state=state))) == 3
    assert srv.created == 2 and "msgbatch_002" in state["done"]

    # 4. Projektstand UND Merkliste sind verloren (Neustart des Containers): Suche bei Anthropic. Der erste
    # Lauf braucht nur „g“ (Probelauf); „h“ und „i“ werden für den nächsten Lauf aufgehoben.
    done["msgbatch_002"] = L._iso()
    interrupted({"done": dict(done)}, notes, "ghi")
    assert srv.created == 3
    fresh = Path(tempfile.mkdtemp()) / "merkliste.json"
    llm = _llm(srv, fresh)
    proj = {}
    llm.bind(proj)
    state = {"done": dict(done)}
    log = []
    assert _collect(llm.run_batch(_jobs("g"), prefix="proj1-w", state=state, log=log.append)) == {"g": "Antwort g"}
    assert srv.created == 3 and len(state["stash"]) == 2 and "msgbatch_003" in state["done"], (srv.created, state, log)
    assert any("wartet auf sein Ende" in m for m in log)
    assert proj["cost_log"][L.STEP_EARLIER]["requests"] == 2, "bezahlt ist bezahlt: steht in der Kostenaufstellung"
    before = llm.cost_usd
    assert _collect(llm.run_batch(_jobs("h", "i"), prefix="proj1-w", state=state)) == {"h": "Antwort h", "i": "Antwort i"}
    assert srv.created == 3 and not state["stash"] and llm.cost_usd == before
    # Ein anderes Projekt übernimmt fremde Sammelaufträge nicht und merkt sich das Ergebnis der Prüfung
    other = _llm(srv, fresh)
    assert _collect(other.run_batch(_jobs("a"), prefix="proj2-w", state={})) == {"a": "Antwort a"}
    assert srv.created == 4 and other.notes.first_id("msgbatch_003").startswith("proj1-w-")

    # 5. Der Aufrufer bricht beim Abholen ab: beim nächsten Mal kommt nur der Rest, nichts wird doppelt gezählt
    srv2 = FakeServer()
    llm = _llm(srv2)
    proj, state, got = {}, {}, []
    llm.bind(proj)
    gen = llm.run_batch(_jobs("a", "b", "c"), prefix="p-w", state=state)
    got.append(next(gen))
    got.append(next(gen))
    gen.close()  # „b“ wurde geliefert, aber nicht mehr als übernommen vermerkt
    assert state["open"]["msgbatch_001"]["seen"] == [L.LLM.custom_id("p-w", _jobs("a")[0])]
    rest = _collect(llm.run_batch(_jobs("b", "c"), prefix="p-w", state=state))
    assert set(rest) == {"b", "c"} and srv2.created == 1
    assert proj["cost_log"]["Testschritt"]["requests"] == 3, "„b“ kommt noch einmal, wird aber nur einmal gezählt"

    # 6. Fortsetzung nach einem Absturz: erst der ältere Sammelauftrag (Anfang), dann der jüngere (Fortsetzung)
    def answer(params, cid):
        return ("Teil zwei.", "end_turn") if cid.endswith("_r1") else ("Teil eins. ", "pause_turn")

    srv3 = FakeServer(answer)
    notes3 = Path(tempfile.mkdtemp()) / "merkliste.json"
    llm = _llm(srv3, notes3)
    state = {}
    gen = llm.run_batch(_jobs("x", web_search=2), prefix="p-w", state=state)
    srv3.fail_running = None
    orig = srv3.polls
    srv3.polls = 0

    class Crash(Exception):
        pass

    def crash_on_second(bid, _retrieve=llm.client.messages.batches.retrieve):
        if bid == "msgbatch_002":
            raise Crash()
        return _retrieve(bid)

    llm.client.messages.batches.retrieve = crash_on_second
    try:
        _collect(gen)
        raise AssertionError("Abbruch erwartet")
    except Crash:
        pass
    srv3.polls = orig
    assert srv3.created == 2 and state["partial"], "die Fortsetzung ist eingereicht, ihr Anfang steht im Projekt"
    # Der Projektstand ist weg; die Merkliste nennt die Fortsetzung, die Liste bei Anthropic auch den Anfang
    llm = _llm(srv3, notes3)
    state = {}
    assert _collect(llm.run_batch(_jobs("x", web_search=2), prefix="p-w", state=state)) == {"x": "Teil eins. Teil zwei."}
    assert srv3.created == 2, "die Fortsetzung wird nicht ein zweites Mal bezahlt"


def test_batch_collect_only():
    """Direkter Ablauf (submit=False): Abgeholt wird nur, was für das Projekt aus einem Sammelauftrag schon
    bezahlt ist; eingereicht wird nichts. Läuft bei Anthropic ein Sammelauftrag, den die App keinem Projekt
    zuordnen kann, hält der Lauf an, bevor er dieselben Aufträge direkt bezahlt."""
    def answer(params, cid):
        return f"Antwort {_prompt_of(params)[-1]}", "end_turn"

    def crashed(srv, notes, keys, prefix="proj1-w", **kw):
        """Reicht im Sparmodus ein und „stürzt“ beim Warten ab; der Projektstand geht dabei verloren."""
        srv.fail_running = KeyboardInterrupt()
        try:
            _collect(_llm(srv, notes).run_batch(_jobs(*keys, **kw), prefix=prefix, state={}))
            raise AssertionError("Abbruch erwartet")
        except KeyboardInterrupt:
            pass
        finally:
            srv.fail_running = None

    def stops(fn) -> str:
        try:
            fn()
        except L.OpenBatchError as ex:
            return str(ex)
        raise AssertionError("der direkte Ablauf hätte anhalten müssen")

    # 1. Nichts bekannt: kein Ergebnis, kein Sammelauftrag, der Merkzettel im Projekt bleibt leer
    srv = FakeServer(answer)
    llm = _llm(srv)
    state = {}
    assert not llm.batch_waiting("proj1-w", None) and not llm.batch_waiting("proj1-w", state)
    assert _collect(llm.run_batch(_jobs("a", "b"), prefix="proj1-w", state=state, submit=False)) == {}
    assert srv.created == 0 and srv.live == 0 and not any(state.values())

    # 2. Eigener, noch laufender Sammelauftrag (die Merkliste des Servers kennt ihn, der Projektstand nicht): Er
    # wird abgewartet und abgeholt. „c“ war nicht dabei und bleibt dem Aufrufer; „b“ braucht dieser Lauf nicht.
    notes = Path(tempfile.mkdtemp()) / "merkliste.json"
    srv = FakeServer(answer, polls=2)
    crashed(srv, notes, "ab")
    llm = _llm(srv, notes)
    proj, state, log = {}, {}, []
    llm.bind(proj)
    assert llm.batch_waiting("proj1-w", state) and not state
    got = _collect(llm.run_batch(_jobs("a", "c"), prefix="proj1-w", state=state, log=log.append, submit=False))
    assert got == {"a": "Antwort a"} and srv.created == 1 and srv.live == 0, got
    assert list(state["done"]) == ["msgbatch_001"] and not state["open"] and len(state["stash"]) == 1
    assert any("wird abgeholt" in m for m in log) and not llm.notes.ids("proj1-w")
    assert proj["cost_log"]["Testschritt"]["batch_requests"] == 1
    assert proj["cost_log"][L.STEP_EARLIER]["batch_requests"] == 1, "bezahlt ist bezahlt"
    # 3. Das aufgehobene Ergebnis wird verwendet, ohne Anfrage bei Anthropic und ohne neue Kosten
    before = llm.cost_usd
    assert llm.batch_waiting("proj1-w", state)
    assert _collect(llm.run_batch(_jobs("b"), prefix="proj1-w", state=state, submit=False)) == {"b": "Antwort b"}
    assert not state["stash"] and llm.cost_usd == before and not llm.batch_waiting("proj1-w", state)

    # 4. Laufender Sammelauftrag unklarer Herkunft (nach einem Neustart ist auch die Merkliste neu): Der direkte
    # Ablauf wartet nicht, hält aber an, bevor er offene Aufträge selbst bezahlt – in jedem Projekt, denn
    # zuordnen lässt sich der Sammelauftrag erst nach seinem Ende.
    srv = FakeServer(answer, polls=2)
    crashed(srv, None, "ab")
    llm = _llm(srv)
    for prefix in ("proj1-w", "proj2-w"):
        msg = stops(lambda: llm.batch_waiting(prefix, {}))
        assert "Sparmodus" in msg and "später noch einmal" in msg and " Uhr" in msg, msg
        stops(lambda: _collect(llm.run_batch(_jobs("a", "b"), prefix=prefix, state={}, submit=False)))
    assert srv.created == 1 and srv.cancelled == 0 and srv.live == 0
    assert srv.batches["msgbatch_001"]["status"] == "in_progress", "der direkte Ablauf wartet nicht"
    # … liegt alles Benötigte schon im Projekt, hält nichts an
    held = {"stash": {L.LLM.custom_id("proj1-w", _jobs("b")[0]): {"text": "aus dem Projekt", "t": L._iso()}}}
    assert llm.batch_waiting("proj1-w", held)
    assert _collect(llm.run_batch(_jobs("b"), prefix="proj1-w", state=held, submit=False)) == {"b": "aus dem Projekt"}
    # Nach seinem Ende erkennt die App den Sammelauftrag an den Auftragskennungen: Ein anderes Projekt arbeitet
    # unbehelligt weiter, das eigene holt die Ergebnisse ab
    srv._finish("msgbatch_001")
    assert not llm.batch_waiting("proj2-w", {})
    assert _collect(llm.run_batch(_jobs("a"), prefix="proj2-w", state={}, submit=False)) == {}
    state = {}
    assert llm.batch_waiting("proj1-w", state)
    assert _collect(llm.run_batch(_jobs("a", "b"), prefix="proj1-w", state=state, submit=False)) == \
        {"a": "Antwort a", "b": "Antwort b"}
    assert srv.created == 1 and srv.live == 0 and "msgbatch_001" in state["done"]

    # 5. Eine unterbrochene Recherche (pause_turn) setzt der direkte Ablauf nicht als Sammelauftrag fort: Der
    # Aufrufer erledigt den Auftrag direkt, im Projekt bleibt kein halber Stand zurück
    def paused(params, cid):
        return ("Teil zwei.", "end_turn") if cid.endswith("_r1") else ("Teil eins. ", "pause_turn")

    notes = Path(tempfile.mkdtemp()) / "merkliste.json"
    srv = FakeServer(paused, polls=2)
    crashed(srv, notes, "x", prefix="p-w", web_search=2)
    llm = _llm(srv, notes)
    state = {}
    assert _collect(llm.run_batch(_jobs("x", web_search=2), prefix="p-w", state=state, submit=False)) == {}
    assert srv.created == 1 and not state["partial"] and not state["open"] and "msgbatch_001" in state["done"]
    assert llm.ledger["Testschritt"]["paused"] == 1, "die bezahlte Teilantwort steht in der Kostenaufstellung"

    # 6. Ist die Liste der Sammelaufträge bei Anthropic nicht abrufbar (z.B. fehlende Berechtigung), läuft der
    # direkte Ablauf einfach weiter; der Sparmodus meldet das Problem
    srv = FakeServer(answer)
    llm = _llm(srv)

    def denied(limit=20):
        raise L.AccountError("keine Berechtigung")

    llm.client.messages.batches.list = denied
    assert not llm.batch_waiting("p-w", {})
    assert _collect(llm.run_batch(_jobs("a"), prefix="p-w", state={}, submit=False)) == {}
    try:
        _collect(llm.run_batch(_jobs("a"), prefix="p-w", state={}))
        raise AssertionError("der Sparmodus hätte das Problem melden müssen")
    except L.AccountError as ex:
        assert "Berechtigung" in str(ex)
    assert srv.created == 0

    # 7. Wechsel auf den zweiten Server: Server A reicht ein und fällt aus, auf Server B geht es mit demselben
    # Projekt weiter. Server B führt seine eigene, ältere Merkliste und kennt den Sammelauftrag nicht. Ohne
    # Sparmodus hält er an; mit Sparmodus wartet er das Ende ab und übernimmt die Ergebnisse, statt dieselben
    # Aufträge ein zweites Mal einzureichen.
    srv = FakeServer(answer, polls=2)
    server_b = Path(tempfile.mkdtemp()) / "merkliste.json"
    L.BatchNotes(server_b).add("anderes-w", "msgbatch_900")
    crashed(srv, None, "ab")  # Server A
    llm = _llm(srv, server_b)
    assert "zweiten Server" in stops(lambda: llm.batch_waiting("proj1-w", {}))
    stops(lambda: _collect(llm.run_batch(_jobs("a", "b"), prefix="proj1-w", state={}, submit=False)))
    assert srv.batches["msgbatch_001"]["status"] == "in_progress" and srv.created == 1
    log = []
    assert _collect(llm.run_batch(_jobs("a", "b"), prefix="proj1-w", state={}, log=log.append)) == \
        {"a": "Antwort a", "b": "Antwort b"}
    assert srv.created == 1 and srv.cancelled == 0 and any("wartet auf sein Ende" in m for m in log), log

    # 8. Zwei Projekte auf demselben Server: Der Sammelauftrag des anderen Projekts steht auf der Merkliste und
    # hält den direkten Ablauf nicht auf – auch dann nicht, wenn er erst in diesem Augenblick eingetragen wird
    srv = FakeServer(answer, polls=2)
    shared = Path(tempfile.mkdtemp()) / "merkliste.json"
    crashed(srv, shared, "ab", prefix="proj9-w")
    llm = _llm(srv, shared)
    assert not llm.batch_waiting("proj1-w", {})
    assert _collect(llm.run_batch(_jobs("a"), prefix="proj1-w", state={}, submit=False)) == {}
    llm.notes.remove("proj9-w", "msgbatch_001")  # eingereicht, aber noch nicht eingetragen
    sleep = L.time.sleep
    L.time.sleep = lambda s: llm.notes.add("proj9-w", "msgbatch_001")
    try:
        assert _collect(llm.run_batch(_jobs("a"), prefix="proj1-w", state={}, submit=False)) == {}
        llm.notes.remove("proj9-w", "msgbatch_001")
        assert not llm.batch_waiting("proj1-w", {})
    finally:
        L.time.sleep = sleep
    assert srv.created == 1 and srv.batches["msgbatch_001"]["status"] == "in_progress"


def test_batch_notes_file():
    path = Path(tempfile.mkdtemp()) / "m.json"
    n = L.BatchNotes(path)
    n.add("p-w", "b1")
    n.add("q-s", "b2")
    assert n.ids("p-w") == ["b1"] and n.all_ids() == {"b1", "b2"}
    n.remove("p-w", "b1")
    assert not n.ids("p-w") and L.BatchNotes(path).all_ids() == {"b2"}
    data = json.loads(path.read_text())
    assert set(data) == {"projects", "seen"} and "b2" in json.dumps(data), "nur Nummern, keine Inhalte"
    broken = L.BatchNotes(Path("/nicht/vorhanden/m.json"))  # nicht beschreibbar: kein Fehler, nur keine Merkliste
    broken.add("p", "b")
    assert broken.ids("p") == []


# ----------------------------------------------------------------------
# Gesamtablauf mit Sparmaßnahmen: gemeinsame Attrappen
# ----------------------------------------------------------------------

LEAN_TEXT = """1. Strengths of the Study
Randomized design with 1,200 patients.
2. Weaknesses and Limitations
Open-label design.
3. Examination of the Authors' Conclusions
Supported.
Conclusion
The primary endpoint was met (HR 0.80; 95% CI 0.70-0.90); the result is valid."""
NEWS_EN = ("The randomized trial TEST (1) examined X in 1,200 patients. The primary endpoint was met "
           "(HR 0.80; 95% CI 0.70-0.90).")
NEWS_DE = ("Die randomisierte Studie TEST (1) prüfte X bei 1.200 Patienten. Der primäre Endpunkt wurde erreicht "
           "(HR 0,80; 95-%-KI 0,70-0,90).")
FULL_HEAD = "Du bist ein erfahrener klinisch-wissenschaftlicher Experte"
OPTS = {"sota": False, "style": True, "transitions": True, "own_refs": True, "joint": True}
_quiet = lambda m: None  # noqa: E731


class Fake(T.FakeLLM):
    """KI-Attrappe wie im Gesamttest, zusätzlich mit schlanker Würdigung und Kurznachricht; zählt die Aufrufe."""

    def __init__(self):
        super().__init__()
        self.n = {"full": 0, "lean": 0, "news": 0, "news_from_lean": 0, "spell": 0, "spell_lines": 0, "formal": 0,
                  "refs": 0, "groups": 0}

    def text(self, prompt, **kw):
        p = prompt if isinstance(prompt, str) else json.dumps(prompt, default=str)
        if "Arbeitsfassung für eine KURZNACHRICHT" in p:
            assert "Websuche" not in p and "web_search" not in kw.get("tools", "")
            assert not kw.get("web_search"), "schlanke Würdigung ohne Websuche"
            self.n["lean"] += 1
            return LEAN_TEXT
        if "eine KURZNACHRICHT auf" in p:
            self.n["news"] += 1
            self.n["news_from_lean"] += "The primary endpoint was met (HR 0.80; 95% CI 0.70-0.90); the result is valid." in p
            return NEWS_EN
        if "vollständig ins" in p and NEWS_EN in p:
            return NEWS_DE
        if FULL_HEAD in p:
            self.n["full"] += 1
        return super().text(prompt, **kw)

    def text_ex(self, prompt, **kw):
        self.n["spell"] += 1
        self.n["spell_lines"] += len(re.findall(r"^\d+\|\|\|", prompt, re.MULTILINE))
        return super().text_ex(prompt, **kw)

    def json(self, prompt, schema, **kw):
        props = schema["properties"]
        if "befunde" in props:
            self.n["formal"] += 1
        elif "entries" in props:
            self.n["refs"] += 1
        elif "groups" in props and "pub_ids" in json.dumps(schema):
            self.n["groups"] += 1
        return super().json(prompt, schema, **kw)


def _answer_from(fake):
    """Antworten der Anthropic-Attrappe aus der KI-Attrappe: gleiche Antwort im direkten Aufruf und im
    Sammelauftrag."""
    def answer(params, cid):
        prompt = _prompt_of(params)
        fmt = (params.get("output_config") or {}).get("format")
        if fmt:
            return json.dumps(fake.json(prompt, fmt["schema"])), "end_turn"
        if "Korrigiere AUSSCHLIESSLICH Rechtschreib" in prompt:
            return fake.text_ex(prompt)[0], "end_turn"
        return fake.text(prompt, web_search=len(params.get("tools") or [])), "end_turn"
    return answer


_PDF: list[bytes] = []
_BASE: dict[str, dict] = {}


def _world(per_query: int = 6) -> None:
    """PubMed und Downloads simulieren; dieselbe Suche liefert stets dieselben Treffer."""
    def search(q, n, email=""):
        seed = zlib.crc32(q.encode("utf-8")) % 90000
        return [str(10_000_000 + seed * 100 + i) for i in range(per_query)], per_query

    if not _PDF:
        _PDF.append(T._pdf_with_image("Leitlinie Empfehlung " * 400))
    pubmed.search = search
    pubmed.fetch_records = lambda ids, email="": {i: T._rec(i) for i in ids}
    pubmed.find_pmid = lambda doi="", title="", email="": None
    pipeline.unpaywall = lambda doi, email: {"is_oa": False}
    pipeline.fetch_oa_pdf = lambda doi, email: None
    guidelines.download_pdf = lambda url: _PDF[0]
    references.pubmed.find_pmid = lambda **kw: None
    recommend.MAX_PER_QUESTION = 3


def _planned(per_query: int = 6) -> dict:
    """Projekt nach dem Einlesen, mit einem festen Suchplan je Thema (noch ohne Bewertung)."""
    key = f"planned{per_query}"
    _world(per_query)
    if key not in _BASE:
        proj = pipeline.new_project()
        proj["email"] = "test@example.org"
        pipeline.run_intake(Fake(), proj, "8 Gastro\n8.1 Thema Eins\n8.2 Thema Zwei", "Hinweise",
                            ("t.docx", T._template()), _quiet)
        for tid, t in proj["topics"].items():
            t["plan"] = {"pico": "P", "exclusion": "", "queries": [
                {"name": "praezise", "query": f"{tid} praezise"}, {"name": "breit", "query": f"{tid} breit"}]}
        _BASE[key] = proj
    return copy.deepcopy(_BASE[key])


def _ranked(project_id: str = "") -> dict:
    """Projekt nach Schritt 1 (Empfehlungen und Leitlinien), bereit für „Manuskript erstellen“."""
    if "ranked" not in _BASE:
        proj = _planned()
        fake = Fake()
        pipeline.run_recommendations(fake, proj, _quiet)
        pipeline.run_guideline_search(fake, proj, _quiet)
        proj["style"]["alt.docx"] = "Früheres Manuskript. " * 300
        assert not proj["errors"], proj["errors"]
        _BASE["ranked"] = proj
    _world()
    proj = copy.deepcopy(_BASE["ranked"])
    if project_id:
        proj["id"] = project_id
    return proj


def _texts(proj: dict, key: str = "rohfassung_DE") -> list[str]:
    return [p.text for p in Document(io.BytesIO(proj["outputs"][key])).paragraphs]


def _singles(proj: dict) -> list[str]:
    return [p for p, d in proj["pubs"].items() if d.get("appraisal_en") and not d.get("group")]


# ----------------------------------------------------------------------
# Sparmodus in Schritt 2: kritische Würdigungen als Sammelauftrag
# ----------------------------------------------------------------------

def test_step2_batch_matches_live():
    """Gleiche Aufträge, gleiches Ergebnis: Das Manuskript aus dem Sammelauftrag stimmt wortgleich mit dem aus
    dem direkten Ablauf überein; die Würdigungen kosten die Hälfte (Gebühr je Websuche unverändert)."""
    fake = Fake()
    srv = FakeServer(_answer_from(fake))
    live = _ranked("live00000001")
    llm = _llm(srv)
    llm.bind(live)
    pipeline.run_manuscript(llm, live, _quiet, dict(OPTS))
    assert not live["errors"], live["errors"]
    assert srv.created == 0 and "batch" not in live, "ohne Sparmodus kein Sammelauftrag"

    log = []
    proj = _ranked("spar00000001")
    llm = _llm(srv)
    llm.bind(proj)
    pipeline.run_manuscript(llm, proj, log.append, {**OPTS, "batch": True})
    assert not proj["errors"], proj["errors"]
    n = len(_singles(proj))
    assert n >= 4 and srv.created == 1 and len(srv.batches["msgbatch_001"]["requests"]) == n
    assert all(r["params"]["tools"][0]["max_uses"] == A.WEB_SEARCHES for r in srv.batches["msgbatch_001"]["requests"])
    assert not proj["batch"]["w"]["open"] and len(proj["batch"]["w"]["done"]) == 1
    assert not any(d.get("appraisal_raw") for d in proj["pubs"].values())
    for p in _singles(live):
        assert proj["pubs"][p]["appraisal_en"] == live["pubs"][p]["appraisal_en"]
        assert proj["pubs"][p]["literature"] == live["pubs"][p]["literature"]
    assert _texts(proj) == _texts(live) and _texts(proj, "rohfassung_EN") == _texts(live, "rohfassung_EN")
    assert any("Sammelauftrag" in m for m in log)
    row, ref = proj["cost_log"][L.STEP_APPRAISAL], live["cost_log"][L.STEP_APPRAISAL]
    assert row["calls"] == ref["calls"] == n and row["batch_requests"] == n and ref["batch_requests"] == 0
    assert abs(ref["usd"] - n * 0.014) < 1e-9 and abs(row["usd"] - n * (0.007 + 0.01)) < 1e-9
    assert abs(row["list_usd"] - n * (0.014 + 0.01)) < 1e-9
    # alles andere läuft im Sparmodus wie gewohnt direkt
    other = lambda log_: {s: r["calls"] for s, r in log_.items() if s != L.STEP_APPRAISAL}  # noqa: E731
    assert other(proj["cost_log"]) == other(live["cost_log"])
    assert abs(proj["cost_usd"] - sum(r["usd"] for r in proj["cost_log"].values())) < 1e-9
    ov = pipeline.cost_overview(proj)
    assert ov["rows"][0]["usd"] >= ov["rows"][-1]["usd"] and abs(ov["total"] - proj["cost_usd"]) < 1e-9
    assert abs(ov["saved"] - n * 0.007) < 1e-9 and ov["earlier"] < 1e-9
    # Ein zweiter Lauf reicht nichts mehr ein
    pipeline.run_manuscript(_llm(srv), proj, _quiet, {**OPTS, "batch": True})
    assert srv.created == 1


def test_step2_batch_resume():
    """Neustart der App während des Sammelauftrags: Der nächste Lauf holt ihn ab, statt neu einzureichen – mit
    dem Projektstand, mit der Merkliste des Servers und, wenn beides fehlt, über die Liste bei Anthropic."""
    fake = Fake()
    for case in ("projekt", "merkliste", "anthropic"):
        srv = FakeServer(_answer_from(fake), polls=2)
        notes = Path(tempfile.mkdtemp()) / "merkliste.json"
        proj = _ranked("neustart0001")
        srv.fail_running = RuntimeError("Absturz")
        try:
            pipeline.run_manuscript(_llm(srv, notes), proj, _quiet, {**OPTS, "batch": True})
            raise AssertionError("der Lauf hätte abbrechen müssen")
        except RuntimeError as ex:
            assert "Absturz" in str(ex)
        assert srv.created == 1 and proj["batch"]["w"]["open"] and not _singles(proj)
        srv.fail_running = None
        if case != "projekt":      # Zwischenstand von vor dem Lauf geladen: das Projekt kennt den Auftrag nicht
            proj = _ranked("neustart0001")
        if case == "anthropic":    # auch die Merkliste ist weg (Neustart des Containers)
            notes = Path(tempfile.mkdtemp()) / "merkliste.json"
        log = []
        pipeline.run_manuscript(_llm(srv, notes), proj, log.append, {**OPTS, "batch": True})
        assert srv.created == 1, f"{case}: kein zweiter Sammelauftrag"
        assert len(_singles(proj)) == len(srv.batches["msgbatch_001"]["requests"]) and not proj["errors"], case
        assert any("früherer Sammelauftrag dieses Projekts wird abgeholt" in m for m in log), (case, log)
        assert proj["outputs"].get("manuskript_DE")


def test_step2_batch_new_material():
    """Kommt nach einem unterbrochenen Sammelauftrag ein Volltext hinzu, übernimmt der nächste Lauf für diese
    Publikation nicht das ältere Ergebnis aus dem Abstract: Die Würdigung entsteht mit dem Volltext neu. Alle
    anderen Ergebnisse werden abgeholt und nicht noch einmal bezahlt."""
    fake = Fake()
    srv = FakeServer(_answer_from(fake), polls=2)
    crashed = _ranked("material0001")
    srv.fail_running = RuntimeError("Absturz")
    try:
        pipeline.run_manuscript(_llm(srv), crashed, _quiet, {**OPTS, "batch": True})
        raise AssertionError("der Lauf hätte abbrechen müssen")
    except RuntimeError as ex:
        assert "Absturz" in str(ex)
    srv.fail_running = None
    n = len(srv.batches["msgbatch_001"]["requests"])
    first = pipeline._pub_list(crashed)[0][0]
    proj = _ranked("material0001")  # Zwischenstand von vor dem Lauf, danach lädt die Autorin einen Volltext hoch
    proj["pubs"].setdefault(first, {})["full_text"] = "Volltext der Publikation mit Methoden und Ergebnissen. " * 200
    llm = _llm(srv)
    llm.bind(proj)
    pipeline.run_manuscript(llm, proj, _quiet, {**OPTS, "batch": True})
    assert srv.created == 2 and not proj["errors"], proj["errors"]
    reqs = srv.batches["msgbatch_002"]["requests"]
    assert len(reqs) == 1 and "Volltext der Publikation mit Methoden" in _prompt_of(reqs[0]["params"])
    assert len(_singles(proj)) == n and proj["pubs"][first]["appraisal_en"]
    # das ältere Ergebnis dieser Publikation ist bezahlt und wird als solches ausgewiesen
    assert proj["cost_log"][L.STEP_EARLIER]["batch_requests"] == 1
    assert proj["cost_log"][L.STEP_APPRAISAL]["batch_requests"] == n
    # Auftragskennung: Publikation und vorhandene Unterlagen, nicht der Wortlaut der Vorgabe
    ids = {r["custom_id"] for r in srv.batches["msgbatch_001"]["requests"]}
    assert reqs[0]["custom_id"] not in ids


def test_step2_batch_raw_and_budget():
    """Eine abgeholte, noch nicht nachbearbeitete Antwort wird beim nächsten Start verwendet und nicht erneut
    bezahlt; das Kostenlimit gilt schon beim Einreichen."""
    fake = Fake()
    srv = FakeServer(_answer_from(fake))
    proj = _ranked("rohtext00001")
    first = pipeline._pub_list(proj)[0][0]
    proj["pubs"].setdefault(first, {})["appraisal_raw"] = {"text": T.APPRAISAL, "kind": ""}
    pipeline.run_manuscript(_llm(srv), proj, _quiet, {**OPTS, "batch": True})
    n = len(_singles(proj))
    assert len(srv.batches["msgbatch_001"]["requests"]) == n - 1 and proj["pubs"][first]["appraisal_en"]
    assert "appraisal_raw" not in proj["pubs"][first] and not proj["errors"]

    # Kostenlimit: geschätzt 0,9 USD je Würdigung – mit 2 USD Limit gehen zwei Aufträge hinaus, der Rest bleibt offen
    srv = FakeServer(_answer_from(fake))
    proj = _ranked("limit0000001")
    try:
        pipeline.run_manuscript(_llm(srv, budget=2.0), proj, _quiet, {**OPTS, "batch": True})
        raise AssertionError("das Kostenlimit hätte greifen müssen")
    except L.BudgetExceeded as ex:
        assert "nicht eingereicht" in str(ex)
    assert len(srv.batches["msgbatch_001"]["requests"]) == 2
    raws = [p for p, d in proj["pubs"].items() if d.get("appraisal_raw")]
    assert len(raws) == 2 and not _singles(proj), "bezahlte Antworten liegen im Projekt"
    # Fortsetzung mit höherem Limit: nur die fehlenden Würdigungen werden eingereicht
    pipeline.run_manuscript(_llm(srv, budget=100.0), proj, _quiet, {**OPTS, "batch": True})
    assert srv.created == 2 and len(srv.batches["msgbatch_002"]["requests"]) == n - 2
    assert len(_singles(proj)) == n and not proj["errors"], proj["errors"]


def test_step2_switch_to_direct():
    """Wechsel vom Sparmodus zum direkten Ablauf in Schritt 2: Was aus einem Sammelauftrag schon bezahlt ist,
    wird übernommen und nicht ein zweites Mal bezahlt – unbearbeitete Antworten im Projekt ebenso wie ein
    Sammelauftrag, der bei Anthropic noch läuft oder dort fertig liegt."""
    def crashed(srv, notes, project_id) -> int:
        """Lauf im Sparmodus, die App „stürzt“ während des Sammelauftrags ab. Rückgabe: Zahl der Aufträge."""
        proj = _ranked(project_id)
        srv.fail_running = RuntimeError("Absturz")
        try:
            pipeline.run_manuscript(_llm(srv, notes), proj, _quiet, {**OPTS, "batch": True})
            raise AssertionError("der Lauf hätte abbrechen müssen")
        except RuntimeError as ex:
            assert "Absturz" in str(ex)
        finally:
            srv.fail_running = None
        return len(srv.batches["msgbatch_001"]["requests"])

    # 1. Das Kostenlimit hält den Sparmodus an: zwei bezahlte Antworten liegen unbearbeitet im Projekt. Der
    # nächste Start läuft ohne Sparmodus und erstellt nur die fehlenden Würdigungen direkt.
    fake = Fake()
    srv = FakeServer(_answer_from(fake))
    proj = _ranked("wechsel00001")
    llm = _llm(srv, budget=2.0)
    llm.bind(proj)
    try:
        pipeline.run_manuscript(llm, proj, _quiet, {**OPTS, "batch": True})
        raise AssertionError("das Kostenlimit hätte greifen müssen")
    except L.BudgetExceeded as ex:
        assert "nicht eingereicht" in str(ex)
    assert sum(1 for d in proj["pubs"].values() if d.get("appraisal_raw")) == 2 and fake.n["full"] == 2
    llm = _llm(srv, budget=100.0)
    llm.bind(proj)
    pipeline.run_manuscript(llm, proj, _quiet, dict(OPTS))
    n = len(_singles(proj))
    assert srv.created == 1 and n >= 4 and not proj["errors"], proj["errors"]
    assert fake.n["full"] == n, "jede Würdigung entsteht genau einmal"
    row = proj["cost_log"][L.STEP_APPRAISAL]
    assert row["calls"] == n and row["batch_requests"] == 2, row
    assert not any(d.get("appraisal_raw") for d in proj["pubs"].values()) and proj["outputs"].get("manuskript_DE")

    # 2. Die App startet während des Sammelauftrags neu, geladen wird der Zwischenstand von vor dem Lauf, und der
    # Schritt startet OHNE Sparmodus (das Häkchen steht in dem älteren Zwischenstand nicht).
    for case in ("merkliste", "anthropic"):
        fake = Fake()
        srv = FakeServer(_answer_from(fake), polls=2)
        notes = Path(tempfile.mkdtemp()) / "merkliste.json"
        n_req = crashed(srv, notes, "wechsel00002")
        if case == "anthropic":  # auch die Merkliste ist weg; der Sammelauftrag ist inzwischen beendet
            notes = Path(tempfile.mkdtemp()) / "merkliste.json"
            srv._finish("msgbatch_001")
        proj, log = _ranked("wechsel00002"), []
        llm = _llm(srv, notes)
        llm.bind(proj)
        pipeline.run_manuscript(llm, proj, log.append, dict(OPTS))
        assert srv.created == 1 and not proj["errors"], (case, proj["errors"])
        assert len(_singles(proj)) == n_req == fake.n["full"], f"{case}: jede Würdigung entsteht genau einmal"
        row = proj["cost_log"][L.STEP_APPRAISAL]
        assert row["calls"] == row["batch_requests"] == n_req, (case, row)
        assert any("früherer Sammelauftrag dieses Projekts wird abgeholt" in m for m in log), (case, log)
        assert list(proj["batch"]["w"]["done"]) == ["msgbatch_001"] and proj["outputs"].get("manuskript_DE")

    # 3. Wie zuvor, aber der Sammelauftrag läuft noch und auch die Merkliste ist weg: Der direkte Ablauf hält an,
    # bevor er eine Würdigung bezahlt. Mit Sparmodus wartet die App das Ende ab und übernimmt die Ergebnisse.
    fake = Fake()
    srv = FakeServer(_answer_from(fake), polls=2)
    n_req = crashed(srv, None, "wechsel00003")
    proj = _ranked("wechsel00003")
    try:
        pipeline.run_manuscript(_llm(srv), proj, _quiet, dict(OPTS))
        raise AssertionError("der direkte Ablauf hätte anhalten müssen")
    except L.OpenBatchError as ex:
        assert "Sparmodus" in str(ex)
    assert fake.n["full"] == 0 and not _singles(proj) and srv.created == 1 and "batch" not in proj
    pipeline.run_manuscript(_llm(srv), proj, _quiet, {**OPTS, "batch": True})
    assert srv.created == 1 and len(_singles(proj)) == n_req == fake.n["full"] and not proj["errors"], proj["errors"]


# ----------------------------------------------------------------------
# Schlanke Würdigung für Kurznachrichten
# ----------------------------------------------------------------------

NEWS_COUNTS = {"long": 1, "mid": 1, "short": 1, "news": 10}


def _lean_project(project_id: str = ""):
    proj = _ranked(project_id)
    proj["tier_counts"] = dict(NEWS_COUNTS)
    return proj


def test_lean_flow():
    fake = Fake()
    proj = _lean_project()
    log = []
    pipeline.run_manuscript(fake, proj, log.append, {**OPTS, "lean_news": True})
    assert not proj["errors"], proj["errors"]
    lean = [p for p in _singles(proj) if pipeline._is_lean(proj, p)]
    grouped = {m for d in proj["pubs"].values() for m in d.get("group") or []}
    news = [p for p, tier in proj["tiers"].items() if tier == "Kurznachricht"]
    assert len(lean) >= 2 and grouped, "die Testdaten enthalten Kurznachrichten und eine gemeinsame Würdigung"
    assert sorted(lean) == sorted(p for p in news if not proj["pubs"][p].get("group")), \
        "schlank genau für die einzeln stehenden Kurznachrichten"
    assert not set(lean) & grouped, "Publikationen einer gemeinsamen Würdigung werden vollständig gewürdigt"
    assert fake.n["lean"] == len(lean) and fake.n["full"] == len(_singles(proj)) - len(lean)
    assert fake.n["news"] == fake.n["news_from_lean"] == len(lean), "Kurznachricht aus der schlanken Würdigung"
    for p in lean:
        d = proj["pubs"][p]
        assert d["appraisal_en"] == LEAN_TEXT and d["body_en"].startswith("1. Strengths of the Study")
        assert d["literature"] == [pubmed.format_reference(proj["records"][p])], "nur die Publikation selbst"
        assert d["short_en"] == NEWS_EN and d["short_de"] == NEWS_DE
    full = [p for p in _singles(proj) if p not in lean]
    assert all(len(proj["pubs"][p]["literature"]) > 1 for p in full)
    text = "\n".join(_texts(proj, "manuskript_DE"))
    assert "Kurznachrichten" in text and "Die randomisierte Studie TEST" in text
    assert any(b["punkt"] == "Schlanke Würdigung für Kurznachrichten" and str(len(lean)) in b["begruendung"]
               for b in proj["outputs"]["befunde"])
    assert any("schlank" in m for m in log)
    # Ohne den Schalter entsteht für dieselben Publikationen die vollständige Würdigung wie bisher
    ref_fake = Fake()
    ref = _lean_project()
    pipeline.run_manuscript(ref_fake, ref, _quiet, dict(OPTS))
    assert ref_fake.n["lean"] == 0 and ref_fake.n["full"] == len(_singles(ref)) == len(_singles(proj))
    assert ref["tiers"] == proj["tiers"], "die Längenplanung hängt nicht von der Art der Würdigung ab"
    # Ein zweiter Lauf erstellt nichts neu
    before = dict(fake.n)
    pipeline.run_manuscript(fake, proj, _quiet, {**OPTS, "lean_news": True})
    assert (fake.n["lean"], fake.n["full"], fake.n["news"]) == (before["lean"], before["full"], before["news"])
    return proj, lean


def test_lean_upgrade():
    """Eine schlank gewürdigte Publikation, die nicht mehr als einzelne Kurznachricht im Manuskript steht, wird
    vollständig gewürdigt: nach geänderter Längenverteilung, beim Abschalten und in einer gemeinsamen Würdigung."""
    base, lean = test_lean_flow()

    # 1. Neue Verteilung: keine Kurznachrichten mehr
    fake = Fake()
    proj = copy.deepcopy(base)
    proj["tier_counts"] = {"long": 1, "mid": 1, "short": 11, "news": 0}  # gleiche Gesamtzahl, andere Längen
    log = []
    pipeline.run_manuscript(fake, proj, log.append, {**OPTS, "lean_news": True})
    assert not proj["errors"], proj["errors"]
    added = set(_singles(proj)) - set(_singles(base))  # mit der neuen Verteilung kann eine Publikation hinzukommen
    assert fake.n["full"] == len(lean) + len(added) and fake.n["lean"] == 0
    assert not any(pipeline._is_lean(proj, p) for p in _singles(proj))
    for p in lean:
        d = proj["pubs"][p]
        assert proj["tiers"][p] != "Kurznachricht" and len(d["literature"]) > 1 and d["short_en"] != NEWS_EN
    assert any("vollständig gewürdigt" in m for m in log)
    assert not any(b["punkt"] == "Schlanke Würdigung für Kurznachrichten" for b in proj["outputs"]["befunde"])

    # 2. Schalter aus: vorhandene schlanke Würdigungen werden ersetzt, die Kurznachricht entsteht neu
    fake = Fake()
    proj = copy.deepcopy(base)
    pipeline.run_manuscript(fake, proj, _quiet, dict(OPTS))
    assert fake.n["full"] == len(lean) and fake.n["news"] == len(lean) and fake.n["news_from_lean"] == 0
    assert not any(pipeline._is_lean(proj, p) for p in _singles(proj)) and not proj["errors"]

    # 3. Gemeinsame Würdigung: die schlank gewürdigte Publikation wird vorher vollständig gewürdigt
    fake = Fake()
    proj = copy.deepcopy(base)
    by_topic = pipeline._singles_by_topic(proj, [(p, t) for p, t in pipeline._pub_list(proj)
                                                 if not any(p in (d.get("group") or []) for d in proj["pubs"].values())])
    tid, members = next((t, ps) for t, ps in by_topic.items() if len(ps) >= 2 and any(p in lean for p in ps))
    pair = [next(p for p in members if p in lean)] + [next(p for p in members if p not in lean or p != members[0])]
    pair = list(dict.fromkeys(pair + members))[:2]
    proj["topics"][tid]["groups"] = list(proj["topics"][tid].get("groups", [])) + [
        {"pmids": pair, "question_de": "Frage", "question_en": "Question"}]
    pipeline.run_manuscript(fake, proj, _quiet, {**OPTS, "lean_news": True})
    gid = "G-" + "-".join(pair)
    assert proj["pubs"][gid]["appraisal_en"] and not proj["errors"], proj["errors"]
    assert not any(pipeline._is_lean(proj, p) for p in pair)
    assert fake.n["full"] == sum(1 for p in pair if p in lean) and fake.n["groups"] == 0
    assert all(pipeline._is_lean(proj, p) for p in lean if p not in pair), "die übrigen bleiben schlank"

    # 4. Schlägt die vollständige Würdigung fehl, bleibt die schlanke Fassung (mit Hinweis)
    class Broken(Fake):
        def text(self, prompt, **kw):
            if FULL_HEAD in prompt and "Arbeitsfassung für eine KURZNACHRICHT" not in prompt:
                raise RuntimeError("KI nicht erreichbar")
            return super().text(prompt, **kw)

    proj = copy.deepcopy(base)
    pipeline.run_manuscript(Broken(), proj, _quiet, dict(OPTS))
    assert all(pipeline._is_lean(proj, p) and proj["pubs"][p]["short_en"] == NEWS_EN for p in lean)
    assert sum("es bleibt bei der schlanken Fassung" in e["where"] for e in proj["errors"]) == len(lean)
    assert proj["outputs"].get("manuskript_DE")


def test_lean_batch():
    """Sparmodus und schlanke Würdigung zusammen: beide Arten von Aufträgen in einem Sammelauftrag; die
    nachträgliche vollständige Würdigung läuft in einem zweiten."""
    fake = Fake()
    srv = FakeServer(_answer_from(fake))
    proj = _lean_project("schlank00001")
    llm = _llm(srv)
    llm.bind(proj)
    pipeline.run_manuscript(llm, proj, _quiet, {**OPTS, "batch": True, "lean_news": True})
    assert not proj["errors"], proj["errors"]
    lean = [p for p in _singles(proj) if pipeline._is_lean(proj, p)]
    reqs = srv.batches["msgbatch_001"]["requests"]
    assert srv.created == 1 and len(reqs) == len(_singles(proj)) and len(lean) >= 2
    assert sum("tools" not in r["params"] for r in reqs) == len(lean), "schlanke Aufträge ohne Websuche"
    assert proj["cost_log"][L.STEP_APPRAISAL_LEAN]["batch_requests"] == len(lean)
    assert proj["cost_log"][L.STEP_APPRAISAL]["batch_requests"] == len(reqs) - len(lean)
    ref = _lean_project()
    pipeline.run_manuscript(Fake(), ref, _quiet, {**OPTS, "lean_news": True})
    assert _texts(proj) == _texts(ref), "wortgleich mit dem direkten Ablauf"
    # Abschalten im Sparmodus: die vollständigen Würdigungen kommen in einem weiteren Sammelauftrag
    pipeline.run_manuscript(_llm(srv), proj, _quiet, {**OPTS, "batch": True})
    assert srv.created == 2 and len(srv.batches["msgbatch_002"]["requests"]) == len(lean)
    assert not any(pipeline._is_lean(proj, p) for p in _singles(proj)) and not proj["errors"]


def test_lean_comparison():
    fake = Fake()
    srv = FakeServer(_answer_from(fake))
    proj = _lean_project("vergleich001")
    llm = _llm(srv)
    llm.bind(proj)
    pipeline.run_lean_comparison(llm, proj, _quiet)
    test = proj["lean_test"]
    assert len(test["items"]) == 3 and "lean_test_work" not in proj and not proj["errors"], proj["errors"]
    assert fake.n["full"] == 3 and fake.n["lean"] == 3 and fake.n["news"] == 6 and fake.n["news_from_lean"] == 3
    for it in test["items"]:
        d = proj["pubs"][it["pmid"]]
        assert d["appraisal_en"] and not pipeline._is_lean(proj, it["pmid"]), "vollständige Würdigung bleibt im Projekt"
        assert it["full_de"] == NEWS_DE and it["lean_de"] == NEWS_DE and it["first"] in ("full", "lean")
    # je Würdigung: vollständig 0,014 USD + Angleichen der Literaturziffern 0,007 USD; schlank 0,014 USD
    assert abs(test["costs"]["full_usd"] - 0.021) < 1e-9 and test["costs"]["full_n"] == 3
    assert abs(test["costs"]["lean_usd"] - 0.014) < 1e-9 and test["costs"]["lean_n"] == 3
    texts = [p.text for p in Document(io.BytesIO(pipeline.lean_comparison_docx(proj))).paragraphs]
    assert sum(t.startswith("Fassung 1: ") for t in texts) == 3 and sum(t.startswith("Fassung 2: ") for t in texts) == 3
    key_at = texts.index("Auflösung")
    assert all("schlanke Würdigung" not in t for t in texts[1:key_at] if t.startswith("Fassung")), "ohne Kennzeichnung"
    assert sum("Fassung 1 = " in t for t in texts[key_at:]) == 3
    # Die drei Publikationen sind als Kurznachricht geplant; der spätere Lauf bezahlt ihre Würdigung nicht erneut
    tiers, grouped = pipeline._preview_tiers(fake, proj, pipeline._pub_list(proj), _quiet, True, {"replan_tiers": True})
    assert all(tiers[it["pmid"]] == "Kurznachricht" and it["pmid"] not in grouped for it in test["items"])
    before = fake.n["full"]
    pipeline.run_manuscript(_llm(srv), proj, _quiet, {**OPTS, "lean_news": True})
    assert fake.n["full"] - before == len(_singles(proj)) - 3 - sum(pipeline._is_lean(proj, p) for p in _singles(proj))
    # Abbruch mitten im Vergleich: der nächste Start setzt fort und bezahlt Fertiges nicht noch einmal
    fake2 = Fake()
    proj = _lean_project("vergleich002")

    class Stop(Fake):
        def text(self, prompt, **kw):
            if "Arbeitsfassung für eine KURZNACHRICHT" in prompt:
                raise L.BudgetExceeded("Limit")
            return fake2.text(prompt, **kw)

    try:
        pipeline.run_lean_comparison(Stop(), proj, _quiet)
        raise AssertionError("der Vergleich hätte anhalten müssen")
    except L.BudgetExceeded:
        pass
    assert fake2.n["full"] == 3 and "lean_test" not in proj and len(proj["lean_test_work"]["pmids"]) == 3
    pipeline.run_lean_comparison(fake2, proj, _quiet)
    assert fake2.n["full"] == 3 and fake2.n["lean"] == 3 and len(proj["lean_test"]["items"]) == 3
    assert pipeline.lean_comparison_docx(pipeline.new_project()) is None


def test_lean_prompt():
    """Die schlanke Vorgabe übernimmt Grundregeln, Abschnitte 1–3, Stil und Schlussprinzip wortgleich aus der
    vollständigen Vorgabe; es fehlen nur die Teile, die eine Kurznachricht nicht enthält."""
    entry = {"title": "TITEL", "journal": "J", "pubdate": "2026", "pub_types": ["RCT"], "abstract": "ABSTRACT",
             "full_text": "VOLLTEXT"}
    block = A._fulltext_block(entry)
    full = prompts.appraisal_prompt(entry, "Englisch", block, "", max_websuchen=4)
    lean = prompts.lean_appraisal_prompt(entry, "Englisch", block)
    parts = prompts._lean_parts()
    for name in ("rules", "sections", "style"):
        assert len(parts[name]) > 500 and parts[name] in full and parts[name] in lean, name
    assert parts["qc_dropped"] == 3 and prompts._LEAN_VOLLTEXT_HINWEIS in full and prompts._LEAN_VOLLTEXT_HINWEIS in lean
    for kept in ("TITEL", "ABSTRACT", "VOLLTEXT", "'Fazit' -> 'Conclusion'", "KEINE LITERATUR", "SCHLUSSPRINZIP",
                 "1. Stärken der Studie", "2. Schwächen und Limitationen",
                 "3. Prüfung der Schlussfolgerungen der Autoren"):
        assert kept in lean, kept
    for gone in ("Websuche", "4. Einordnung in den wissenschaftlichen Gesamtkontext", "Fazit für die Praxis",
                 "Kommentar", "Neuigkeit"):
        assert gone in full and gone not in lean, gone
    assert len(lean) < 0.75 * len(full)
    assert prompts._LEAN_VOLLTEXT_HINWEIS not in prompts.lean_appraisal_prompt({**entry, "full_text": ""}, "Englisch", "")
    job, lean_job = A.appraisal_job(entry, [], key="w|1"), A.lean_appraisal_job(entry, key="wl|1")
    assert job.prompt == full and job.web_search == A.WEB_SEARCHES and job.step == L.STEP_APPRAISAL
    assert lean_job.prompt == lean and lean_job.web_search == 0 and lean_job.step == L.STEP_APPRAISAL_LEAN
    assert lean_job.model == job.model and lean_job.effort == job.effort == "high"
    try:
        A.finish_lean_appraisal("  ")
        raise AssertionError("leere Antwort muss abgelehnt werden")
    except ValueError:
        pass


# ----------------------------------------------------------------------
# Sparmodus in Schritt 1: Bewertung der Suchtreffer und Leitliniensuche als Sammelauftrag
# ----------------------------------------------------------------------

def _step1_view(proj: dict) -> dict:
    return {tid: {"recommended": t.get("recommended"), "reserve": t.get("reserve"),
                  "ranking": {p: (it.get("score"), it.get("priority")) for p, it in (t.get("ranking") or {}).items()},
                  "n": t.get("n_candidates"), "guidelines": [g.get("title") for g in t.get("guidelines") or []]}
            for tid, t in proj["topics"].items()}


def test_step1_batch_matches_live():
    fake = Fake()
    srv = FakeServer(_answer_from(fake))
    live = _planned(10)
    llm = _llm(srv)
    llm.bind(live)
    pipeline.run_recommendations(llm, live, _quiet)
    pipeline.run_guideline_search(llm, live, _quiet)
    assert srv.created == 0 and not live["errors"], live["errors"]

    log = []
    proj = _planned(10)
    proj["id"] = "schritt1spar"
    llm = _llm(srv)
    llm.bind(proj)
    pipeline.run_recommendations(llm, proj, log.append, batch=True)
    pipeline.run_guideline_search(llm, proj, _quiet)
    assert not proj["errors"], proj["errors"]
    topics = [t for t in proj["topics"].values() if t.get("n_candidates")]
    reqs = srv.batches["msgbatch_001"]["requests"]
    n_rank = sum(1 for r in reqs if "format" in r["params"]["output_config"])
    n_gl = sum(1 for r in reqs if "tools" in r["params"])
    assert srv.created == 1 and n_rank == 2 * len(topics) and n_gl == len(topics) and len(reqs) == n_rank + n_gl, \
        "je Thema 20 Kandidaten in zwei Aufträgen (15 + 5) und eine Leitliniensuche"
    assert _step1_view(proj) == _step1_view(live), "gleiche Empfehlungen und Leitlinien wie im direkten Ablauf"
    assert not any("rank_cache" in t or "guidelines_raw" in t for t in proj["topics"].values())
    assert any("Sparmodus" in m for m in log)
    rank, ref = proj["cost_log"][L.STEP_RANK], live["cost_log"][L.STEP_RANK]
    assert rank["batch_requests"] == n_rank and rank["calls"] == ref["calls"], "Nachbewertungen laufen direkt"
    assert rank["usd"] < ref["usd"] and abs(rank["list_usd"] - ref["list_usd"]) < 1e-9
    gl = proj["cost_log"][L.STEP_GUIDELINES]
    assert gl["batch_requests"] == n_gl and gl["calls"] == live["cost_log"][L.STEP_GUIDELINES]["calls"] == 2 * n_gl
    # nichts mehr offen: ein weiterer Lauf reicht nichts ein
    pipeline.run_recommendations(_llm(srv), proj, _quiet, batch=True)
    assert srv.created == 1


def test_failed_topic_is_retried():
    """Direkter Ablauf: Schlägt die Literaturrecherche eines Themas fehl, bleibt es offen (keine leere
    Empfehlung), und der nächste Start von Schritt 1 wiederholt genau dieses Thema. Das gilt auch für
    Zwischenstände, in denen ein älterer Programmstand dem Thema bereits eine leere Empfehlung eingetragen hat."""
    class Boom(Fake):
        fail = True

        def json(self, prompt, schema, **kw):
            if self.fail and "Thema Zwei" in prompt and "relevance" in json.dumps(schema):
                raise RuntimeError("kaputt")
            return super().json(prompt, schema, **kw)

    fake = Boom()
    proj = _planned(10)
    pipeline.run_recommendations(fake, proj, _quiet)
    failed = [tid for tid, t in proj["topics"].items() if t.get("rec_error")]
    assert len(failed) == 1 and len(proj["errors"]) == 1, proj["errors"]
    t = proj["topics"][failed[0]]
    assert "recommended" not in t and "ranking" not in t and "window" not in t
    done = {tid: list(x["recommended"]) for tid, x in proj["topics"].items() if x.get("n_candidates")}
    assert len(done) >= 2
    old = copy.deepcopy(proj)  # Zustand, den der Programmstand bis 2026-11-13.1 hinterlassen hat
    old["topics"][failed[0]].update({"recommended": [], "reserve": [], "background": []})
    fake.fail = False
    for state in (proj, old):
        pipeline.run_recommendations(fake, state, _quiet)
        t = state["topics"][failed[0]]
        assert not t.get("rec_error") and t["n_candidates"] == 20 and len(t["ranking"]) == 20 and t["window"]
    ref = _planned(10)
    pipeline.run_recommendations(Fake(), ref, _quiet)
    assert _step1_view(proj) == _step1_view(ref) == _step1_view(old)


def test_step1_batch_failure_and_resume():
    """Schlägt ein Bewertungsauftrag fehl, bleibt das Thema offen; die bezahlten Bewertungen der übrigen Aufträge
    stehen im Projekt, und der nächste Lauf reicht nur den fehlenden Auftrag ein. Ein Neustart während des
    Sammelauftrags führt nicht zu einem zweiten."""
    fake = Fake()
    seen = {"n": 0}

    def answer(params, cid):
        if cid and "format" in params["output_config"]:
            seen["n"] += 1
            if seen["n"] == 2:
                return "errored"
        return _answer_from(fake)(params, cid)

    srv = FakeServer(answer)
    proj = _planned(10)
    proj["id"] = "schritt1fehl"
    pipeline.run_recommendations(_llm(srv), proj, _quiet, batch=True)
    failed = [tid for tid, t in proj["topics"].items() if t.get("rec_error")]
    assert len(failed) == 1 and "recommended" not in proj["topics"][failed[0]]
    cache = proj["topics"][failed[0]]["rank_cache"]
    assert len(cache["chunks"]) == 1, "die Bewertungen des anderen Auftrags sind im Projekt gesichert"
    assert all(t.get("guidelines_raw") for t in proj["topics"].values() if t.get("n_candidates") or t.get("rec_error"))
    pipeline.run_recommendations(_llm(srv), proj, _quiet, batch=True)
    assert srv.created == 2 and len(srv.batches["msgbatch_002"]["requests"]) == 1, "nur der fehlende Auftrag"
    t = proj["topics"][failed[0]]
    assert not t.get("rec_error") and "rank_cache" not in t and t["n_candidates"] == 20 and len(t["ranking"]) == 20
    pipeline.run_guideline_search(fake, proj, _quiet)
    ref = _planned(10)
    pipeline.run_recommendations(Fake(), ref, _quiet)
    pipeline.run_guideline_search(Fake(), ref, _quiet)
    assert _step1_view(proj) == _step1_view(ref)

    # Neustart während des Sammelauftrags (Zwischenstand von vor dem Lauf, Merkliste des Servers vorhanden)
    srv = FakeServer(_answer_from(fake), polls=2)
    notes = Path(tempfile.mkdtemp()) / "merkliste.json"
    proj = _planned(10)
    proj["id"] = "schritt1neu0"
    srv.fail_running = RuntimeError("Absturz")
    try:
        pipeline.run_recommendations(_llm(srv, notes), proj, _quiet, batch=True)
        raise AssertionError("der Lauf hätte abbrechen müssen")
    except RuntimeError as ex:
        assert "Absturz" in str(ex)
    srv.fail_running = None
    proj = _planned(10)
    proj["id"] = "schritt1neu0"
    pipeline.run_recommendations(_llm(srv, notes), proj, _quiet, batch=True)
    assert srv.created == 1, "die Aufträge behalten ihre Kennung, der laufende Sammelauftrag wird übernommen"
    pipeline.run_guideline_search(fake, proj, _quiet)
    assert _step1_view(proj) == _step1_view(ref)


def test_step1_switch_to_direct():
    """Wechsel vom Sparmodus zum direkten Ablauf in Schritt 1: Bezahlte Bewertungen im Projekt und ein
    Sammelauftrag bei Anthropic werden übernommen; nur was fehlt, läuft direkt."""
    def is_rank(params) -> bool:
        fmt = (params.get("output_config") or {}).get("format")
        return bool(fmt) and fmt["schema"] == recommend.RANK_SCHEMA

    def crashed(srv, notes, project_id) -> int:
        """Lauf im Sparmodus, die App „stürzt“ während des Sammelauftrags ab. Rückgabe: Zahl der Aufträge."""
        proj = _planned(10)
        proj["id"] = project_id
        srv.fail_running = RuntimeError("Absturz")
        try:
            pipeline.run_recommendations(_llm(srv, notes), proj, _quiet, batch=True)
            raise AssertionError("der Lauf hätte abbrechen müssen")
        except RuntimeError as ex:
            assert "Absturz" in str(ex)
        finally:
            srv.fail_running = None
        return len(srv.batches["msgbatch_001"]["requests"])

    ref = _planned(10)
    pipeline.run_recommendations(Fake(), ref, _quiet)
    pipeline.run_guideline_search(Fake(), ref, _quiet)

    # 1. Im Sammelauftrag scheitert ein Bewertungsauftrag; der nächste Start läuft ohne Sparmodus
    fake = Fake()
    seen = {"n": 0}
    direct = []  # Bewertungsaufträge, die direkt (zum vollen Preis) an die KI gehen

    def answer(params, cid):
        if is_rank(params):
            if not cid:
                direct.append(_prompt_of(params))
            else:
                seen["n"] += 1
                if seen["n"] == 2:
                    return "errored"
        return _answer_from(fake)(params, cid)

    srv = FakeServer(answer)
    proj = _planned(10)
    proj["id"] = "schritt1wech"
    llm = _llm(srv)
    llm.bind(proj)
    pipeline.run_recommendations(llm, proj, _quiet, batch=True)
    failed = [tid for tid, t in proj["topics"].items() if t.get("rec_error")]
    assert len(failed) == 1 and len(proj["topics"][failed[0]]["rank_cache"]["chunks"]) == 1
    chunks = [_prompt_of(r["params"]) for r in srv.batches["msgbatch_001"]["requests"] if is_rank(r["params"])]
    lost, n_direct, n_err, log = chunks[1], len(direct), len(proj["errors"]), []
    half = copy.deepcopy(proj)
    llm = _llm(srv)
    llm.bind(proj)
    pipeline.run_recommendations(llm, proj, log.append)
    assert srv.created == 1, "der direkte Ablauf reicht nichts ein"
    assert [q for q in direct[n_direct:] if q in chunks] == [lost], \
        "nur der gescheiterte Auftrag läuft direkt; bezahlte Bewertungen werden nicht wiederholt"
    assert any("früherer Sammelauftrag" in m for m in log), log
    t = proj["topics"][failed[0]]
    assert not t.get("rec_error") and "rank_cache" not in t and len(t["ranking"]) == 20
    pipeline.run_guideline_search(llm, proj, _quiet)
    assert _step1_view(proj) == _step1_view(ref) and len(proj["errors"]) == n_err, proj["errors"]

    # 2. Die App startet während des Sammelauftrags neu (Zwischenstand von vor dem Lauf, die Merkliste des
    # Servers ist erhalten); der nächste Start läuft ohne Sparmodus und übernimmt den ganzen Sammelauftrag
    fake = Fake()
    srv = FakeServer(_answer_from(fake), polls=2)
    notes = Path(tempfile.mkdtemp()) / "merkliste.json"
    n_req = crashed(srv, notes, "schritt1wec2")
    proj = _planned(10)
    proj["id"] = "schritt1wec2"
    llm = _llm(srv, notes)
    llm.bind(proj)
    pipeline.run_recommendations(llm, proj, _quiet)
    pipeline.run_guideline_search(llm, proj, _quiet)
    assert srv.created == 1 and not proj["errors"], proj["errors"]
    rank, gl = proj["cost_log"][L.STEP_RANK], proj["cost_log"][L.STEP_GUIDELINES]
    assert rank["batch_requests"] + gl["batch_requests"] == n_req, "der ganze Sammelauftrag wurde übernommen"
    assert _step1_view(proj) == _step1_view(ref)
    assert not any("rank_cache" in t or "guidelines_raw" in t for t in proj["topics"].values())

    # 3. Ein direkter Lauf ohne Vorgeschichte hinterlässt keinen Merkzettel für Sammelaufträge
    plain = _planned(10)
    pipeline.run_recommendations(_llm(FakeServer(_answer_from(Fake()))), plain, _quiet)
    assert "batch" not in plain and not plain["errors"]

    # 4. Der Sammelauftrag läuft noch und auch die Merkliste ist weg: Der direkte Ablauf hält sofort an – noch
    # vor der PubMed-Suche und vor jedem KI-Aufruf. Mit Sparmodus wartet die App und übernimmt die Ergebnisse.
    fake = Fake()
    srv = FakeServer(_answer_from(fake), polls=2)
    n_req = crashed(srv, None, "schritt1wec3")
    proj = _planned(10)
    proj["id"] = "schritt1wec3"
    searched, search, live = [], pubmed.search, srv.live
    pubmed.search = lambda q, n, email="": (searched.append(q), search(q, n, email))[1]
    try:
        pipeline.run_recommendations(_llm(srv), proj, _quiet)
        raise AssertionError("der direkte Ablauf hätte anhalten müssen")
    except L.OpenBatchError as ex:
        assert "Sparmodus" in str(ex)
    finally:
        pubmed.search = search
    assert not searched and srv.live == live and srv.created == 1 and "batch" not in proj
    llm = _llm(srv)
    llm.bind(proj)
    pipeline.run_recommendations(llm, proj, _quiet, batch=True)
    pipeline.run_guideline_search(llm, proj, _quiet)
    assert srv.created == 1 and not proj["errors"], proj["errors"]
    rank, gl = proj["cost_log"][L.STEP_RANK], proj["cost_log"][L.STEP_GUIDELINES]
    assert rank["batch_requests"] + gl["batch_requests"] == n_req and _step1_view(proj) == _step1_view(ref)

    # 5. Tage später: Der Sammelauftrag ist bei Anthropic gelöscht, im Projekt stehen nur noch die bezahlten
    # Bewertungen. Der direkte Lauf verwendet sie und hinterlässt keinen leeren Merkzettel.
    proj = half
    del proj["batch"]
    srv = FakeServer(answer)
    n_direct = len(direct)
    pipeline.run_recommendations(_llm(srv), proj, _quiet)
    assert [q for q in direct[n_direct:] if q in chunks] == [lost] and srv.created == 0 and "batch" not in proj
    assert "rank_cache" not in proj["topics"][failed[0]] and len(proj["topics"][failed[0]]["ranking"]) == 20


# ----------------------------------------------------------------------
# Zwischenstand speichern und laden
# ----------------------------------------------------------------------

def _reload(proj: dict) -> dict:
    return pipeline.import_project(pipeline.export_project(proj))


def test_state_roundtrip():
    """Zwischenstand speichern und laden: Kostenaufstellung, Sammelaufträge, Merkfunktion und Vergleich bleiben
    erhalten, und der nächste Lauf bezahlt nichts doppelt."""
    fake = Fake()
    srv = FakeServer(_answer_from(fake))
    proj = _lean_project("zwischenst01")
    llm = _llm(srv)
    llm.bind(proj)
    pipeline.run_lean_comparison(llm, proj, _quiet)
    pipeline.run_manuscript(llm, proj, _quiet, {**OPTS, "batch": True, "lean_news": True})
    pipeline.note_run(proj, "Manuskript wird erstellt", llm)
    assert not proj["errors"], proj["errors"]
    loaded = _reload(proj)
    for key in ("id", "cost_usd", "cost_log", "cost_runs", "batch", "build_cache", "lean_test", "tiers"):
        assert loaded[key] == proj[key], key
    assert pipeline.cost_overview(loaded) == pipeline.cost_overview(proj) and pipeline.lean_comparison_docx(loaded)
    before, created, live = dict(fake.n), srv.created, srv.live
    llm = _llm(srv)
    llm.bind(loaded)
    pipeline.run_manuscript(llm, loaded, _quiet, {**OPTS, "batch": True, "lean_news": True})
    assert srv.created == created and not loaded["errors"], loaded["errors"]
    assert {k: fake.n[k] - before[k] for k in ("full", "lean", "news", "spell", "formal", "refs", "groups")} == \
        dict.fromkeys(("full", "lean", "news", "spell", "formal", "refs", "groups"), 0), "nichts wird neu bezahlt"
    assert _texts(loaded, "manuskript_DE") == _texts(proj, "manuskript_DE")
    assert srv.live - live < 12, "nur die wenigen Schritte ohne Merkfunktion laufen erneut"
    assert abs(loaded["cost_usd"] - sum(r["usd"] for r in loaded["cost_log"].values())) < 1e-9
    # Sehr alter Zwischenstand ohne Projektkennung: dieselbe Datei erhält bei jedem Laden dieselbe Kennung
    old = json.dumps({k: v for k, v in pipeline.new_project().items() if k != "id"}).encode("utf-8")
    ids = {pipeline.import_project(old)["id"] for _ in range(3)}
    assert len(ids) == 1 and len(ids.pop()) == 12
    assert pipeline.import_project(old + b" ")["id"] != pipeline.import_project(old)["id"]

    # Lauf bricht während des Sammelauftrags ab, Zwischenstand danach gespeichert, App neu gestartet (auch die
    # Merkliste des Servers ist weg): Der Projektstand allein genügt, um den Sammelauftrag abzuholen.
    srv = FakeServer(_answer_from(fake), polls=2)
    proj = _ranked("zwischenst02")
    srv.fail_running = RuntimeError("Absturz")
    try:
        pipeline.run_manuscript(_llm(srv), proj, _quiet, {**OPTS, "batch": True})
        raise AssertionError("der Lauf hätte abbrechen müssen")
    except RuntimeError as ex:
        assert "Absturz" in str(ex)
    srv.fail_running = None
    loaded = _reload(proj)
    assert loaded["batch"]["w"]["open"] and loaded["batch"] == proj["batch"]
    pipeline.run_manuscript(_llm(srv), loaded, _quiet, {**OPTS, "batch": True})
    assert srv.created == 1 and loaded["outputs"].get("manuskript_DE") and not loaded["errors"], loaded["errors"]

    # Schritt 1: bezahlte Bewertungen eines unvollständigen Themas überstehen das Speichern und Laden
    seen = {"n": 0}

    def answer(params, cid):
        if cid and "format" in params["output_config"]:
            seen["n"] += 1
            if seen["n"] == 2:
                return "errored"
        return _answer_from(fake)(params, cid)

    srv = FakeServer(answer)
    proj = _planned(10)
    proj["id"] = "zwischenst03"
    proj["search_window"] = ["2025-03-01", "2026-03-31"]  # manuell festgelegter Zeitraum gehört zur Kennung
    pipeline.run_recommendations(_llm(srv), proj, _quiet, batch=True)
    failed = [tid for tid, t in proj["topics"].items() if t.get("rec_error")]
    assert len(failed) == 1 and len(proj["topics"][failed[0]]["rank_cache"]["chunks"]) == 1
    loaded = _reload(proj)
    assert loaded["topics"][failed[0]]["rank_cache"] == proj["topics"][failed[0]]["rank_cache"]
    pipeline.run_recommendations(_llm(srv), loaded, _quiet, batch=True)
    assert srv.created == 2 and len(srv.batches["msgbatch_002"]["requests"]) == 1, "nur der fehlende Auftrag"
    t = loaded["topics"][failed[0]]
    assert not t.get("rec_error") and "rank_cache" not in t and len(t["ranking"]) == 20


# ----------------------------------------------------------------------
# Merkfunktion für das erneute Erstellen
# ----------------------------------------------------------------------

def test_build_caches():
    fake = Fake()
    proj = _ranked()
    pipeline.run_manuscript(fake, proj, _quiet, dict(OPTS))
    first = dict(fake.n)
    assert first["spell_lines"] > 20 and first["formal"] == 1 and first["refs"] == 1, first
    assert proj["build_cache"]["spell"] and proj["build_cache"]["formal"] and proj["build_cache"]["refs"]
    texts = _texts(proj, "manuskript_DE")
    # 1. Unverändertes Manuskript: kein KI-Aufruf für Rechtschreibung, formale Prüfung und Literaturverzeichnis
    pipeline.run_manuscript(fake, proj, _quiet, dict(OPTS))
    assert {k: fake.n[k] - first[k] for k in ("spell", "spell_lines", "formal", "refs")} == \
        {"spell": 0, "spell_lines": 0, "formal": 0, "refs": 0}
    assert _texts(proj, "manuskript_DE") == texts and len(proj["outputs"]["befunde"]) > 5
    # 2. Eine Publikation fällt weg: Die Literaturziffern aller folgenden ändern sich, geprüft werden trotzdem
    # nur die wenigen wirklich neuen Textstellen
    second = dict(fake.n)
    tid, drop = next((t, p) for p, t in pipeline._pub_list(proj)
                     if sum(1 for _, tt in pipeline._pub_list(proj) if tt == t) >= 3)
    topic = proj["topics"][tid]
    topic["selected"] = [p for p in topic.get("selected", topic["recommended"]) if p != drop]
    pipeline.run_manuscript(fake, proj, _quiet, dict(OPTS))
    assert drop not in [p for p, _ in pipeline._pub_list(proj)]
    assert fake.n["refs"] == second["refs"], "alle verbliebenen Literatureinträge sind bekannt"
    assert fake.n["spell_lines"] - second["spell_lines"] <= 0.25 * first["spell_lines"], \
        (fake.n["spell_lines"] - second["spell_lines"], first["spell_lines"])
    # 3. Der Zwischenstand nimmt die Merkliste mit
    again = pipeline.import_project(pipeline.export_project(proj))
    assert again["build_cache"]["spell"] == proj["build_cache"]["spell"]


def test_spell_cache_details():
    """Die Merkliste gilt je Textstelle ohne ihre laufenden Nummern: eine gemerkte Korrektur wird mit den
    aktuellen Literaturziffern wieder eingesetzt; passt sie nicht mehr, wird neu geprüft."""
    class Speller:
        def __init__(self):
            self.lines = []

        def text_ex(self, prompt, **kw):
            numbered = re.findall(r"^(\d+)\|\|\|(.*)$", prompt, re.MULTILINE)
            self.lines += [t for _, t in numbered]
            return "\n".join(f"{n}|||{t.replace('Tset', 'Test')}" for n, t in numbered), False

    def doc_of(*paras):
        d = Document()
        for t in paras:
            d.add_paragraph(t)
        return d

    cache, llm = {}, Speller()
    d1 = doc_of("Dies ist ein Tset mit einem Verweis (3) und einem zweiten (4,5).", "Ein fehlerfreier Absatz (2).",
                "12. Autor A. Ein Tset im Titel. J. 2026;1:1.")
    assert formal.fix_spelling_and_grammar(d1, llm, cache) == 2 and len(llm.lines) == 3 and len(cache) == 3
    assert d1.paragraphs[0].text == "Dies ist ein Test mit einem Verweis (3) und einem zweiten (4,5)."
    d2 = doc_of("Dies ist ein Tset mit einem Verweis (7) und einem zweiten (8,9).", "Ein fehlerfreier Absatz (1).",
                "9. Autor A. Ein Tset im Titel. J. 2026;1:1.", "Ein neuer Absatz mit Tset.")
    assert formal.fix_spelling_and_grammar(d2, llm, cache) == 3
    assert llm.lines[3:] == ["Ein neuer Absatz mit Tset."], "nur die neue Textstelle geht an die KI"
    assert d2.paragraphs[0].text == "Dies ist ein Test mit einem Verweis (7) und einem zweiten (8,9)."
    assert d2.paragraphs[1].text == "Ein fehlerfreier Absatz (1)."
    assert d2.paragraphs[2].text == "9. Autor A. Ein Test im Titel. J. 2026;1:1."
    assert d2.paragraphs[3].text == "Ein neuer Absatz mit Test."
    norm, marks = formal._spell_norm("12. Titel (3) und (4,5).")
    assert marks == ["12", "(3)", "(4,5)"] and formal._spell_restore(norm, ["9", "(1)", "(2)"]) == "9. Titel (1) und (2)."
    assert formal._spell_restore(norm, ["9"]) is None
    # ohne Merkliste wie bisher
    d3 = doc_of("Dies ist ein Tset mit einem Verweis (3).")
    assert formal.fix_spelling_and_grammar(d3, llm) == 1 and formal.fix_spelling_and_grammar(d3, None, cache) == 0
    # Obergrenze: die ältesten Einträge fallen weg
    small = {f"k{i}": "" for i in range(formal.SPELL_CACHE_MAX)}
    formal.fix_spelling_and_grammar(doc_of("Noch ein ganz anderer Absatz."), llm, small)
    assert len(small) == formal.SPELL_CACHE_MAX and "k0" not in small
    # Literaturverzeichnis: nur neue Einträge gehen an die KI; die englische Fassung kostet nichts mehr
    fake, refs_cache = Fake(), {}
    rules = {"citation_style": "Vancouver", "citation_examples": ""}
    entries = [f"Autor {i}. Titel {i}. J. 2026;1:{i}." for i in range(5)]
    assert references.apply_style(fake, entries, rules, cache=refs_cache) == entries and fake.n["refs"] == 1
    assert references.apply_style(fake, entries, rules, cache=refs_cache) == entries and fake.n["refs"] == 1
    references.apply_style(fake, entries + ["Neu N. Neuer Titel. J. 2026;2:9."], rules, cache=refs_cache)
    assert fake.n["refs"] == 2 and len(refs_cache) == 6
    references.apply_style(fake, entries, {**rules, "citation_style": "APA"}, cache=refs_cache)
    assert fake.n["refs"] == 3, "anderer Zitierstil: neu formatieren"


# ----------------------------------------------------------------------
# Kostenaufstellung je Arbeitsschritt
# ----------------------------------------------------------------------

def test_cost_log_and_runs():
    fake = Fake()
    srv = FakeServer(_answer_from(fake))
    llm = _llm(srv)
    proj = {"cost_usd": 1.5}  # älteres Projekt: Kosten aus der Zeit vor der Aufstellung
    llm.bind(proj)
    A.kurznachricht(llm, "Würdigung", "Englisch", "Abstract")
    A.translate_structured(llm, NEWS_EN)
    llm.text("ohne Zuordnung", model=L.MODEL_FAST, step="Eigener Schritt")
    assert set(proj["cost_log"]) == {"Kurznachricht", "Übersetzung", "Eigener Schritt"}, set(proj["cost_log"])
    assert abs(proj["cost_usd"] - 1.5 - llm.cost_usd) < 1e-9
    pipeline.note_run(proj, "Testlauf", llm)
    assert proj["cost_runs"][-1]["label"] == "Testlauf" and abs(proj["cost_runs"][-1]["usd"] - llm.cost_usd) < 1e-3
    assert set(proj["cost_runs"][-1]["steps"]) == set(proj["cost_log"])
    ov = pipeline.cost_overview(proj)
    assert [r["step"] for r in ov["rows"]][0] == "Kurznachricht" and abs(ov["earlier"] - 1.5) < 1e-9
    assert abs(ov["total"] - llm.cost_usd) < 1e-9 and ov["saved"] == 0 and len(ov["runs"]) == 1
    for _ in range(pipeline.COST_RUNS_MAX + 5):
        pipeline.note_run(proj, "x", llm)
    assert len(proj["cost_runs"]) == pipeline.COST_RUNS_MAX
    pipeline.note_run(proj, "ohne KI", _llm(srv))
    assert proj["cost_runs"][-1]["label"] == "x", "ein Lauf ohne KI-Aufruf wird nicht notiert"
    assert pipeline.cost_overview({}) == {"rows": [], "total": 0, "earlier": 0.0, "saved": 0.0, "runs": []}
    # ein abgebrochener Lauf: die Kosten stehen schon im Projekt
    blob = pipeline.export_project({**pipeline.new_project(), **proj})
    assert pipeline.import_project(blob)["cost_log"]["Kurznachricht"]["calls"] == 1


def test_step_names_cover_all_calls():
    """Jeder KI-Aufruf der App erscheint in der Kostenaufstellung unter einem verständlichen Namen: entweder
    nennt der Aufruf seinen Arbeitsschritt selbst (step=…), oder seine Funktion steht in llm.STEP_NAMES."""
    missing, sites = [], 0
    for path in sorted((ROOT / "medupdate").glob("*.py")):
        module = path.stem
        if module == "llm":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, owner, prefix=""):
            nonlocal sites
            for child in ast.iter_child_nodes(node):
                name = owner
                if owner is None and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = prefix + child.name
                if isinstance(child, ast.ClassDef) and owner is None:
                    walk(child, None, prefix + child.name + ".")
                    continue
                if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                        and child.func.attr in ("text", "json", "text_ex")
                        and isinstance(child.func.value, ast.Name) and child.func.value.id == "llm"):
                    sites += 1
                    if not any(k.arg == "step" for k in child.keywords) and f"{module}.{name}" not in L.STEP_NAMES:
                        missing.append(f"{module}.{name} (Zeile {child.lineno})")
                walk(child, name, prefix)

        walk(tree, None)
    assert sites >= 40, sites
    assert not missing, f"KI-Aufrufe ohne Arbeitsschritt in llm.STEP_NAMES: {missing}"
    for key in L.STEP_NAMES:  # jeder Eintrag verweist auf eine vorhandene Funktion (fällt bei Umbenennungen auf)
        module, func = key.split(".", 1)
        tree = ast.parse((ROOT / "medupdate" / f"{module}.py").read_text(encoding="utf-8"))
        assert any(isinstance(n, ast.FunctionDef) and n.name == func for n in tree.body), key
    assert len(set(L.STEP_NAMES.values())) <= 30 and L.STEP_APPRAISAL != L.STEP_APPRAISAL_LEAN


def test_app_wiring():
    """Die App reicht die Schalter an den Ablauf weiter und schreibt die Kosten über LLM.bind mit (app.py lässt
    sich ohne Streamlit-Sitzung nicht ausführen; geprüft wird der Quelltext)."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    ast.parse(src)
    for needle in ('llm.bind(proj)', 'pipeline.note_run(proj, label, llm)', 'pipeline.cost_overview(proj)',
                   '"batch": bool(opt_batch)', '"lean_news": bool(opt_lean)', 'batch=_batch1', 'batch=_batch)',
                   'pipeline.run_lean_comparison(_tlm, proj, log)', 'pipeline.lean_comparison_docx(proj)'):
        assert needle in src, needle
    assert 'proj["cost_usd"] = proj.get("cost_usd", 0.0) + job["spent"]' not in src, "Kosten nicht doppelt buchen"
    assert src.count("ANTHROPIC_API_KEY") == 1 and "sk-ant-" not in src


if __name__ == "__main__":
    test_batch_notes_file()
    test_batch_basic_and_cost()
    test_batch_pause_and_errors()
    test_batch_budget_and_stop()
    test_batch_recovery()
    test_batch_collect_only()
    test_lean_prompt()
    test_step_names_cover_all_calls()
    test_app_wiring()
    test_cost_log_and_runs()
    test_spell_cache_details()
    test_step1_batch_matches_live()
    test_failed_topic_is_retried()
    test_step1_batch_failure_and_resume()
    test_step1_switch_to_direct()
    test_step2_batch_matches_live()
    test_step2_batch_resume()
    test_step2_batch_new_material()
    test_step2_batch_raw_and_budget()
    test_step2_switch_to_direct()
    test_lean_upgrade()  # ruft test_lean_flow auf
    test_lean_batch()
    test_lean_comparison()
    test_build_caches()
    test_state_roundtrip()
    print("OK")
