#!/usr/bin/env bash
# Setzt das Passwort des Admin-Kontos in Open WebUI neu.
#
# Läuft im Container "open-webui" und nutzt dessen eigene bcrypt-Bibliothek, der
# Hash entspricht also genau dem, was Open WebUI selbst erzeugt. Das Passwort wird
# verdeckt abgefragt und erscheint weder im Terminal noch in einer Datei oder im Log.
#
# Voraussetzung: genau ein Konto mit Rolle admin. Bei mehreren Konten die E-Mail als
# erstes Argument angeben:  scripts/reset_admin_password.sh admin@example.invalid
set -euo pipefail

CONTAINER=open-webui
EMAIL_ARG="${1:-}"

if ! docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "Container $CONTAINER nicht gefunden." >&2
  exit 1
fi

docker exec -it -e EMAIL_ARG="$EMAIL_ARG" "$CONTAINER" python - <<'EOF'
import getpass, os, sqlite3, sys, time

import bcrypt

db = sqlite3.connect("/app/backend/data/webui.db")
email_arg = os.environ.get("EMAIL_ARG", "").strip().lower()
if email_arg:
    rows = db.execute("select id, email from user where lower(email) = ?", (email_arg,)).fetchall()
else:
    rows = db.execute("select id, email from user where role = 'admin'").fetchall()

if not rows:
    sys.exit("Kein passendes Konto gefunden.")
if len(rows) > 1:
    sys.exit("Mehrere Admin-Konten. Bitte die E-Mail als Argument angeben.")

user_id, email = rows[0]
masked = email[:2] + "***" + email[email.find("@"):] if "@" in email else "***"
print(f"Konto: {masked}")

pw = getpass.getpass("Neues Passwort: ")
if pw != getpass.getpass("Wiederholen:    "):
    sys.exit("Passwörter stimmen nicht überein. Nichts geändert.")
if len(pw) < 8:
    sys.exit("Mindestens 8 Zeichen. Nichts geändert.")
if len(pw.encode("utf-8")) > 72:
    sys.exit("Höchstens 72 Bytes. Nichts geändert.")

hashed = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
updated = db.execute("update auth set password = ? where id = ?", (hashed, user_id)).rowcount
if updated != 1:
    sys.exit("Kein Eintrag in der Tabelle auth aktualisiert. Nichts geändert.")
db.execute("update user set updated_at = ? where id = ?", (int(time.time()), user_id))
db.commit()
print("Passwort gesetzt. Bestehende Sitzungen bleiben gültig.")
EOF
