#!/bin/bash
# Holt den neuesten Programmstand aus GitHub (Branch main) und startet die App nur neu, wenn sich etwas
# geändert hat. Läuft nachts um 4 Uhr und nach jedem Neustart des Servers (myluo-update.timer).
set -euo pipefail
BASE=/opt/myluo
APP=$BASE/app
# shellcheck disable=SC1091
source $BASE/github.env  # GITHUB_TOKEN (nur Lesezugriff auf das Repository)
AUTH="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 -w0)"
cd $APP
OLD=$(git rev-parse HEAD)
git -c safe.directory=$APP -c "http.https://github.com/.extraheader=$AUTH" fetch -q origin main
NEW=$(git -c safe.directory=$APP rev-parse origin/main)
if [ "$OLD" = "$NEW" ]; then
  exit 0
fi
git -c safe.directory=$APP reset -q --hard "$NEW"
REQ=$(sha1sum requirements.txt | cut -d' ' -f1)
if [ "$REQ" != "$(cat $BASE/.requirements 2>/dev/null)" ]; then
  $BASE/venv/bin/python -m pip install -q -r requirements.txt
  echo "$REQ" > $BASE/.requirements
fi
install -m 755 $APP/deploy/update.sh $BASE/update.sh
chown -R myluo:myluo $APP $BASE/venv
systemctl restart myluo.service
echo "Aktualisiert auf $(git -c safe.directory=$APP log --oneline -1)"
