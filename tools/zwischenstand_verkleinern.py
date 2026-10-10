"""Verkleinert einen alten, sehr großen Zwischenstand von medupdate 2 (mit PDFs und Bildern),
damit er sich wieder in die App laden lässt. Die Texte bleiben vollständig erhalten.

Aufruf (nur Python, keine Zusatzpakete):
    python tools/zwischenstand_verkleinern.py medupdate2_zwischenstand_2026-10-10.json
Ergebnis: dieselbe Datei mit der Endung .klein.json.gz daneben.
"""

import gzip
import json
import sys
from pathlib import Path


def main(path: str) -> None:
    src = Path(path)
    raw = src.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    proj = json.loads(raw.decode("utf-8"))
    for d in (proj.get("pubs") or {}).values():
        d.pop("pdf", None)
        d.pop("images", None)
    out = src.with_name(src.name.split(".json")[0] + ".klein.json.gz")
    out.write_bytes(gzip.compress(json.dumps(proj, ensure_ascii=False).encode("utf-8"), 6))
    print(f"Fertig: {out} ({out.stat().st_size / 1e6:.1f} MB statt {src.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
