"""Offline-Test der Bedienoberfläche (app.py): Die App wird wie im Browser bedient (Häkchen setzen, Knöpfe
drücken, auf den Hintergrund-Lauf warten). Anthropic, PubMed und Downloads sind simuliert; es entstehen keine
Kosten. Geprüft werden die Sparmaßnahmen vom 13.11.2026: Sparmodus in Schritt 1 und 2, schlanke Würdigung,
Vergleich und Kostenaufstellung.
Aufruf: python tests/test_app_offline.py  (oder python -m pytest tests/)"""

import logging
import re
import sys
import tempfile
import time
from pathlib import Path

_REAL_SLEEP = time.sleep  # das Testmodul der Sparmaßnahmen schaltet Wartezeiten ab; die Oberfläche braucht sie

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from streamlit.testing.v1 import AppTest  # noqa: E402

import test_savings_offline as S  # noqa: E402 – gemeinsame Attrappen (Anthropic, PubMed, Beispielprojekte)

from medupdate import llm as L, pipeline  # noqa: E402

APP = str(ROOT / "app.py")


class _Frame(S.Fake):
    """KI-Attrappe, die zusätzlich die Kapitelanalyse für den Suchplan beantwortet."""

    def json(self, prompt, schema, **kw):
        if "anchor_query" in schema["properties"]:
            ids = re.findall(r"^(T\d+): ", prompt, re.M)
            return {"chapter_en": "Gastroenterology", "structure": "krankheit", "structure_note": "x",
                    "anchor_query": "gastro[tiab]", "journals": ["Gut"],
                    "topics": [{"id": i, "title_en": f"topic {i}", "searchable": True, "aspect_query": "", "note": ""}
                               for i in ids]}
        return super().json(prompt, schema, **kw)


class _Bench:
    """Simulierte Umgebung für einen Test: Die App erhält statt des echten KI-Zugangs einen mit Attrappe."""

    def __enter__(self):
        self.fake = _Frame()
        self.srv = srv = S.FakeServer(S._answer_from(self.fake))
        self.saved = (time.sleep, L._poll_seconds, L.LLM, logging.root.manager.disable)
        real = L.LLM

        class TestLLM(real):
            def __init__(self, key, budget_usd=0.0):
                super().__init__("x", budget_usd=budget_usd)
                self.client = srv.client()
                self.notes = L.BatchNotes(Path(tempfile.mkdtemp()) / "merkliste.json")

        time.sleep = _REAL_SLEEP
        L._poll_seconds = lambda waited: 0.02
        L.LLM = TestLLM
        logging.disable(logging.WARNING)  # Hinweise von Streamlit zum Betrieb ohne Server
        return self

    def __exit__(self, *exc):
        time.sleep, L._poll_seconds, L.LLM = self.saved[:3]
        logging.disable(self.saved[3])
        return False

    def open(self, proj: dict) -> AppTest:
        at = AppTest.from_file(APP, default_timeout=120)
        at.session_state["proj"] = proj
        at.run()
        _ok(at)
        [t for t in at.sidebar.text_input if "API" in t.label][0].set_value("dummy").run()
        _ok(at)
        return at


def _ok(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]


def _one(at: AppTest, kind: str, start: str):
    found = [x for x in getattr(at, kind) if x.label.startswith(start)]
    assert len(found) == 1, (start, [x.label for x in getattr(at, kind)])
    return found[0]


def _run_job(at: AppTest, element) -> list[str]:
    """Knopf drücken und warten, bis der Hintergrund-Lauf beendet ist; liefert die Erfolgs- und Fehlermeldungen."""
    seen, started = [], time.time()
    element.click().run()
    while True:
        _ok(at)
        seen += [s.value for s in at.success] + ["FEHLER: " + e.value for e in at.error]
        if not at.status:
            return seen
        assert time.time() - started < 120, "der Lauf wurde nicht fertig"
        _REAL_SLEEP(0.2)
        at.run()


def _sidebar(at: AppTest) -> list[str]:
    return [c.value for c in at.sidebar.caption]


def test_app_start_page():
    with _Bench():
        at = AppTest.from_file(APP, default_timeout=120)
        at.run()
        _ok(at)
        assert _one(at, "checkbox", "Sparmodus: Treffer als Sammelauftrag bewerten").value is False
        assert not any("Kosten dieses Projekts" in c for c in _sidebar(at)), "ohne Kosten keine Aufstellung"


def test_app_step2_savings():
    """Themenzuordnung prüfen, Vergleich erstellen, Manuskript im Sparmodus mit schlanker Würdigung."""
    with _Bench() as bench:
        proj = S._lean_project("oberflaeche1")
        at = bench.open(proj)
        compare = _one(at, "button", "🧪 Vergleich für 3 Publikationen")
        assert compare.disabled, "der Vergleich braucht wie das Manuskript die geprüfte Themenzuordnung"
        assert _one(at, "checkbox", "Sparmodus: kritische Würdigungen").value is False
        assert _one(at, "checkbox", "Schlanke Würdigung für Kurznachrichten").value is False, "nur auf Wunsch"

        assert _run_job(at, _one(at, "button", "🧩 Hochladen und Themenzuordnung prüfen")) == \
            ["Themenzuordnung wird geprüft – fertig."]
        assert proj["check_ok"] and not proj["errors"], proj["errors"]

        # Vergleich: drei Publikationen, Word-Datei und gemessene Kosten
        compare = _one(at, "button", "🧪 Vergleich für 3 Publikationen")
        assert not compare.disabled
        assert _run_job(at, compare) == ["Vergleich wird erstellt – fertig."]
        assert len(proj["lean_test"]["items"]) == 3 and not proj["errors"], proj["errors"]
        assert any(b.label == "⬇️ Vergleich als Word-Datei" for b in at.get("download_button"))
        assert any(c.value.startswith("Gemessen: vollständige Würdigung") for c in at.caption)
        assert bench.srv.created == 0, "der Vergleich läuft direkt"

        # Manuskript: Sparmodus und schlanke Würdigung, vollständiger Lauf
        _one(at, "checkbox", "Sparmodus: kritische Würdigungen").check()
        _one(at, "checkbox", "Schlanke Würdigung für Kurznachrichten").check()
        _one(at, "checkbox", "Probelauf").uncheck()
        _one(at, "number_input", "Kostenlimit für diesen Lauf").set_value(100)
        assert _run_job(at, _one(at, "button", "✍️ Manuskript automatisch erstellen")) == \
            ["Manuskript wird erstellt – fertig."]
        assert not proj["errors"], proj["errors"]
        assert proj["outputs"].get("manuskript_DE") and "Ergebnis" in [s.value for s in at.subheader]
        assert proj["batch_step2"] is True and proj["lean_news"] is True
        singles = S._singles(proj)
        lean = [p for p in singles if pipeline._is_lean(proj, p)]
        done = {it["pmid"] for it in proj["lean_test"]["items"]}
        assert lean and not set(lean) & done, "die drei Publikationen des Vergleichs behalten die vollständige Würdigung"
        assert bench.srv.created == 1, "alle fehlenden Würdigungen in einem Sammelauftrag"
        assert len(bench.srv.batches["msgbatch_001"]["requests"]) == len(singles) - 3
        # Kostenaufstellung in der Seitenleiste
        side = _sidebar(at)
        row = next(c for c in side if c.startswith("**Kritische Würdigung**"))
        assert f"davon {len(singles) - 3 - len(lean)} im Sammelauftrag" in row and "Websuche" in row, row
        assert any(c.startswith("**Kritische Würdigung (schlank, für Kurznachrichten)**") for c in side)
        assert any(c.startswith("Ersparnis durch den Sparmodus bisher: ") for c in side)
        runs = next(c for c in side if c.startswith("Letzte Läufe: "))
        assert runs.index("Manuskript wird erstellt") < runs.index("Vergleich wird erstellt"), "neuester Lauf zuerst"
        total = next(c for c in side if c.startswith("KI-Kosten dieses Projekts bisher"))
        assert f"{proj['cost_usd']:.2f}".replace(".", ",") in total
        # Die Schalter behalten ihre Einstellung für den nächsten Lauf
        assert _one(at, "checkbox", "Sparmodus: kritische Würdigungen").value is True
        assert _one(at, "checkbox", "Schlanke Würdigung für Kurznachrichten").value is True
        assert _one(at, "checkbox", "Probelauf").value is False, "nach einem fertigen Manuskript kein Probelauf mehr"


def test_app_step1_savings():
    """Sparmodus in Schritt 1: über den Suchplan und über „Literatursuche starten“."""
    with _Bench() as bench:
        proj = S._planned(10)
        proj["id"] = "oberflaeche2"
        pipeline.run_search_plan(bench.fake, proj, S._quiet)
        at = bench.open(proj)
        assert any(e.label.startswith("🔎 Suchplan prüfen – 4 Themen, 4 noch nicht bewertet") for e in at.expander)
        go = at.button(key="plan_go")
        assert "1,6–3,2 USD" in go.label and not go.disabled, go.label
        at.checkbox(key="batch_step1_plan_0").check().run()
        _ok(at)
        go = at.button(key="plan_go")
        assert "1,1–2,1 USD" in go.label, "im Sparmodus etwa ein Drittel weniger"
        assert _run_job(at, go) == ["Literatur wird bewertet – fertig."]
        assert proj["batch_step1"] is True and not proj["errors"], proj["errors"]
        reqs = bench.srv.batches["msgbatch_001"]["requests"]
        assert bench.srv.created == 1 and len(reqs) == 12, "8 Bewertungsaufträge und 4 Leitliniensuchen"
        assert all(t.get("n_candidates") == 20 and t.get("guidelines") for tid, t in proj["topics"].items()
                   if tid.startswith("T"))
        assert at.button(key="plan_go").disabled, "nichts mehr offen"
        assert at.checkbox(key="batch_step1_plan_1").value is True

        # Eine geänderte Suche öffnet das Thema wieder; „Literatursuche starten“ übernimmt den Sparmodus
        first = next(iter(proj["topics"]))
        proj["topics"][first]["plan_rev"] = proj["topics"][first].get("plan_rev", 0) + 1
        at.run()
        _ok(at)
        assert not at.button(key="plan_go").disabled
        _one(at, "checkbox", "Suchbegriffe vor der Bewertung prüfen").uncheck()
        form_box = [c for c in at.checkbox if c.label.startswith("Sparmodus: Treffer")
                    and not (c.key or "").startswith("batch_step1_plan")]
        assert len(form_box) == 1 and form_box[0].value is True, "Einstellung aus dem Projekt"
        assert _run_job(at, _one(at, "button", "🔍 Literatursuche starten")) == ["Schritt 1 läuft – fertig."]
        assert bench.srv.created == 2 and len(bench.srv.batches["msgbatch_002"]["requests"]) == 2, \
            "nur die zwei Bewertungsaufträge des geänderten Themas"
        assert not proj["errors"], proj["errors"]
        row = next(c for c in _sidebar(at) if c.startswith(f"**{L.STEP_RANK}**"))
        assert "davon 10 im Sammelauftrag" in row, row  # 8 + 2 Bewertungsaufträge; Nachbewertungen laufen direkt


def test_app_failed_topic():
    """Ein Thema, dessen Literaturrecherche fehlgeschlagen ist, wird mit Hinweis angezeigt und kann wiederholt
    werden – auch in dem Zustand, den Programmstände bis 2026-11-13.1 hinterlassen haben."""
    with _Bench() as bench:
        proj = S._planned(10)
        proj["id"] = "oberflaeche3"
        pipeline.run_search_plan(bench.fake, proj, S._quiet)
        pipeline.run_recommendations(bench.fake, proj, S._quiet)
        first = next(iter(proj["topics"]))
        t = proj["topics"][first]
        for k in ("recommended", "reserve", "ranking", "window", "n_candidates", "background"):
            t.pop(k, None)
        t["rec_error"] = "RuntimeError: kaputt"
        at = bench.open(proj)
        for old_state in (False, True):
            if old_state:
                t.update({"recommended": [], "reserve": [], "background": []})
                at.run()
                _ok(at)
            assert sum("Die Literaturrecherche ist für dieses Thema fehlgeschlagen" in e.value for e in at.error) == 1
            assert not at.button(key="plan_go").disabled, "das Thema kann erneut bewertet werden"
        assert _run_job(at, at.button(key="plan_go")) == ["Literatur wird bewertet – fertig."]
        assert not t.get("rec_error") and t["n_candidates"] == 20 and t["window"], t.get("rec_error")
        assert not any("fehlgeschlagen" in e.value for e in at.error)


if __name__ == "__main__":
    test_app_start_page()
    test_app_step2_savings()
    test_app_step1_savings()
    test_app_failed_topic()
    print("OK")
