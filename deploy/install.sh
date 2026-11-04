#!/bin/bash
# Einrichtung des zweiten Produktionsservers (Hetzner Cloud, Ubuntu 24.04). Wird einmal von cloud-init
# aufgerufen (siehe hetzner-cloud-config.yaml), kann aber gefahrlos erneut laufen.
# Erwartet: /opt/myluo/app (geklonter Code), /opt/myluo/secrets.toml, /opt/myluo/github.env (Lese-Token).
set -euo pipefail
BASE=/opt/myluo
APP=$BASE/app

timedatectl set-timezone Europe/Berlin || true
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git python3-venv python3-pip caddy ufw curl

id myluo >/dev/null 2>&1 || useradd --system --home-dir $BASE/home --shell /usr/sbin/nologin myluo
mkdir -p $BASE/home && chown myluo:myluo $BASE/home

# Programmbibliotheken
[ -x $BASE/venv/bin/python ] || python3 -m venv $BASE/venv
$BASE/venv/bin/python -m pip install -q --upgrade pip
$BASE/venv/bin/python -m pip install -q -r $APP/requirements.txt
sha1sum $APP/requirements.txt | cut -d' ' -f1 > $BASE/.requirements

# Geheimnisse nur auf dem Server, nie im Repository
mkdir -p $APP/.streamlit
ln -sf $BASE/secrets.toml $APP/.streamlit/secrets.toml
chown -R myluo:myluo $APP $BASE/venv
chown myluo:myluo $BASE/secrets.toml && chmod 600 $BASE/secrets.toml
chown root:root $BASE/github.env && chmod 600 $BASE/github.env

# Adresse mit HTTPS-Zertifikat: <IP mit Bindestrichen>.sslip.io zeigt auf diesen Server
IP=$(curl -fsS http://169.254.169.254/hetzner/v1/metadata/public-ipv4 || hostname -I | awk '{print $1}')
HOST="${IP//./-}.sslip.io"
cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
	encode gzip
	reverse_proxy 127.0.0.1:8501
}
CADDY
echo "https://$HOST" > $BASE/ADRESSE.txt

cat > /etc/systemd/system/myluo.service <<UNIT
[Unit]
Description=MyLuo Pro (Streamlit)
After=network-online.target
Wants=network-online.target

[Service]
User=myluo
WorkingDirectory=$APP
ExecStart=$BASE/venv/bin/python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# Aktualisierung aus GitHub: nachts um 4 Uhr und nach jedem Neustart des Servers
install -m 755 $APP/deploy/update.sh $BASE/update.sh
cat > /etc/systemd/system/myluo-update.service <<UNIT
[Unit]
Description=MyLuo Pro aus GitHub aktualisieren
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$BASE/update.sh
UNIT
cat > /etc/systemd/system/myluo-update.timer <<UNIT
[Unit]
Description=MyLuo Pro nachts und nach dem Start aktualisieren

[Timer]
OnCalendar=*-*-* 04:00:00
OnBootSec=1min

[Install]
WantedBy=timers.target
UNIT

ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

systemctl daemon-reload
systemctl enable --now myluo.service myluo-update.timer
systemctl restart caddy
echo "MyLuo Pro läuft unter $(cat $BASE/ADRESSE.txt)"
