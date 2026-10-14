#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(realpath -- "$0")")"
if [[ $EUID == 0 ]]; then
    echo 'Run this script as your desktop user, without sudo.' >&2
    exit 1
fi
uuid=schenker-usbc@jerrinot
destination=${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$uuid
install -d -m 0755 "$destination"
install -m 0644 metadata.json extension.js presentation.js "$destination/"
# Queue enablement even if Shell has not discovered this new extension yet.
python3 - <<'PY'
from gi.repository import Gio
settings = Gio.Settings.new('org.gnome.shell')
uuid = 'schenker-usbc@jerrinot'
enabled = settings.get_strv('enabled-extensions')
if uuid not in enabled:
    settings.set_strv('enabled-extensions', enabled + [uuid])
disabled = settings.get_strv('disabled-extensions')
if uuid in disabled:
    settings.set_strv('disabled-extensions', [item for item in disabled if item != uuid])
Gio.Settings.sync()
PY
echo 'Installed and enabled for your next login. Log out and back in to load the new code.'
