#!/usr/bin/env bash
# Installs the FUSE daemon, the Nautilus extension and the systemd user service.
set -euo pipefail

cd "$(dirname "$0")"

cli=$(command -v proton-drive || true)
if [[ -z $cli ]]; then
  echo "proton-drive not found. Install it first: yay -S proton-drive-cli-git" >&2
  exit 1
fi
if ! /usr/bin/python -c "import pyfuse3, trio" 2>/dev/null; then
  echo "Missing Python packages. Install them first: sudo pacman -S python-pyfuse3 python-trio" >&2
  exit 1
fi

share="$HOME/.local/share/proton-drive-nautilus"
mkdir -p "$share" "$HOME/.local/share/nautilus-python/extensions" "$HOME/.config/systemd/user" "$HOME/ProtonDrive"
rm -rf "$share/pdfs" "$share/protondrivefs"
cp -r protondrivefs "$share/protondrivefs"
cp nautilus/proton_drive.py "$HOME/.local/share/nautilus-python/extensions/proton_drive.py"
sed "s|@CLI@|$cli|" systemd/proton-drive-fs.service > "$HOME/.config/systemd/user/proton-drive-fs.service"

bookmarks="$HOME/.config/gtk-3.0/bookmarks"
mkdir -p "$(dirname "$bookmarks")"
touch "$bookmarks"
if ! grep -q "^file://$HOME/ProtonDrive " "$bookmarks"; then
  echo "file://$HOME/ProtonDrive Proton Drive" >> "$bookmarks"
fi

plugin="$HOME/.config/omarchy/plugins/regin.proton-drive"
if command -v omarchy >/dev/null; then
  mkdir -p "$plugin"
  cp omarchy/regin.proton-drive/* "$plugin/"
  if ! grep -q '"regin.proton-drive"' "$HOME/.config/omarchy/shell.json" 2>/dev/null; then
    omarchy bar put regin.proton-drive --section right
  fi
fi

systemctl --user daemon-reload
systemctl --user enable proton-drive-fs.service
systemctl --user restart proton-drive-fs.service
nautilus -q 2>/dev/null || true

echo "Installed. Proton Drive is mounted at ~/ProtonDrive and bookmarked in Nautilus."
echo "Logs: journalctl --user -u proton-drive-fs -f"
