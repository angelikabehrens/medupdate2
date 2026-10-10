"""HTTP-Hilfen (aus MyLuo übernommen)."""

import time

import requests

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/128.0 Safari/537.36",
    "Accept": "application/pdf,text/html,*/*",
}


def request_with_retry(method: str, url: str, retries: int = 3, backoff: float = 2.0, **kwargs) -> requests.Response:
    """Führt eine Anfrage aus und wiederholt sie bei kurzzeitigen Serverfehlern
    (z.B. 502/503 bei PubMed), statt sofort mit einem Fehler abzubrechen."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, url, **kwargs)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(backoff * attempt)
                continue
            r.raise_for_status()
            return r
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise last_error
