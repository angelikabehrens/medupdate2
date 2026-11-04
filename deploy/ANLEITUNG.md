# MyLuo Pro: zweiter und dritter Weg

Streamlit Community Cloud bleibt das Hauptsystem. Bei einer Störung arbeiten Sie mit dem eigenen
Server bei Hetzner weiter, und im Notfall auf Ihrem Mac. Alle drei nutzen denselben Programmstand aus
GitHub. Ein heruntergeladener **Zwischenstand** lässt sich in jedem der drei Systeme laden, Sie können
also mitten in einem Projekt wechseln.

---

## A. Eigener Server bei Hetzner (einmalig, ca. 30 Minuten)

### 1. Lese-Schlüssel für GitHub anlegen (damit der Server den Programmcode holen darf)

1. github.com → oben rechts Ihr Bild → **Settings** → ganz unten **Developer settings**
2. **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
3. Token name: `MyLuo Server`; Expiration: längste mögliche Laufzeit
4. Repository access: **Only select repositories** → `medupdate2`
5. Permissions → **Add permissions** → im Suchfeld `Contents` eingeben und anhaken → daneben **Read-only**
   wählen (Metadata: Read-only kommt automatisch dazu; alles andere bleibt aus)
6. **Generate token**, den Schlüssel (beginnt mit `github_pat_`) kopieren und kurz in einer Notiz
   auf dem Mac ablegen. Er wird nur einmal angezeigt.

### 2. Eigenen API-Schlüssel für den Server anlegen (empfohlen)

In der Anthropic-Konsole einen neuen Schlüssel „MyLuo Server“ anlegen. So lässt er sich unabhängig
von Streamlit Cloud sperren. Ebenfalls kurz in der Notiz ablegen.

### 3. Server bestellen

1. Konto anlegen unter **console.hetzner.cloud** (Zahlungsdaten nötig), Projekt „MyLuo“ anlegen
2. **Server hinzufügen**
   - Standort: **Nürnberg** oder **Falkenstein**
   - Image: **Ubuntu 24.04**
   - Typ: **Shared vCPU, x86**, die kleinste Größe (2 vCPU, 4 GB RAM, ca. 4–6 € im Monat)
   - Networking: **Öffentliche IPv4** eingeschaltet lassen
   - SSH-Key: leer lassen (das Root-Passwort kommt per Mail, Sie brauchen es nur im Notfall)
   - **Cloud config**: den Inhalt der Datei `deploy/hetzner-cloud-config.yaml` vollständig einfügen
     und **direkt in diesem Feld** die drei Platzhalter ersetzen:
     - `HIER_GITHUB_TOKEN_EINFUEGEN` → GitHub-Schlüssel aus Schritt 1
     - `HIER_API_SCHLUESSEL_EINFUEGEN` → API-Schlüssel aus Schritt 2
     - `HIER_ZUGANGSCODE_EINFUEGEN` → Zugangscode für die App (wie bei Streamlit Cloud oder neu)
     Die Anführungszeichen um die Werte stehen lassen.
   - Name: `myluo-pro`
3. **Kostenpflichtig erstellen**. Danach die Notiz mit den Schlüsseln löschen.

### 4. Adresse der App

Die Einrichtung läuft danach von selbst und dauert etwa 10 Minuten. Die Adresse ergibt sich aus der
IPv4-Adresse des Servers (steht in der Hetzner-Konsole), mit Bindestrichen statt Punkten:

IPv4 `49.12.34.56` → **https://49-12-34-56.sslip.io**

Diese Adresse als Lesezeichen speichern. Die Verbindung ist verschlüsselt (HTTPS), der Zugangscode
schützt die App wie bei Streamlit Cloud.

### Im Betrieb

- **Aktualisierung:** automatisch jede Nacht um 4 Uhr und nach jedem Neustart des Servers, aber nur,
  wenn es in GitHub einen neuen Programmstand gibt. Sofort aktualisieren: Hetzner-Konsole → Server →
  **Power** → **Neustart** (nicht während eines laufenden Manuskripts).
- **Nachts keine Läufe starten, die über 4 Uhr hinausgehen**, wenn am Vortag ein neuer Programmstand
  hochgeladen wurde.
- **Daten:** Der Server speichert keine Projekte. Wie bei Streamlit Cloud den Zwischenstand herunterladen.
- **Kosten:** nur der Server (ca. 4–6 € im Monat). Pausieren ist bei Hetzner nicht möglich, nur Löschen.
- **Schlüssel ändern:** Hetzner-Konsole → Server → **Konsole** öffnen, als `root` anmelden, dann
  `nano /opt/myluo/secrets.toml`, Wert ändern, speichern (Ctrl+O, Enter, Ctrl+X), dann
  `systemctl restart myluo`.
- **Wenn die Seite nicht erscheint:** in der Konsole `cat /var/log/myluo-install.log` und
  `systemctl status myluo` eingeben und mir einen Screenshot schicken.
- **GitHub-Schlüssel abgelaufen:** neuen Schlüssel wie in Schritt 1 anlegen, in der Konsole
  `nano /opt/myluo/github.env` öffnen und den Wert ersetzen.

---

## B. Notfall-Start auf dem Mac (kostenlos)

Einmalig:

1. Python installieren von **python.org/downloads/macos** (aktuelle Version 3.13, Installer ausführen).
2. Den Programmordner `medupdate2` auf dem Mac haben (Ihr VS-Code-Ordner aus GitHub). Falls nicht
   vorhanden: in VS Code „Git: Clone“ → `angelikabehrens/medupdate2`.

Starten:

1. Im Finder im Ordner `medupdate2` auf **MyLuo_Pro_starten.command** doppelklicken.
   Meldet macOS „kann nicht geöffnet werden“: Rechtsklick → **Öffnen** → **Öffnen**.
2. Beim ersten Start werden die Programmbibliotheken installiert (einige Minuten), danach öffnet sich
   die App im Browser unter **http://localhost:8501**.
3. Den API-Schlüssel links im Feld „Anthropic-API-Schlüssel“ eingeben. Er wird nicht gespeichert.
4. Solange das Terminal-Fenster offen ist, bleibt der Mac wach. Zum Beenden das Fenster schließen.

Die Startdatei holt jedes Mal den neuesten Programmstand aus GitHub.
