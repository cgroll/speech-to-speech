#!/usr/bin/env bash
# One-time system setup for ydotool (text injection on GNOME/Wayland), needed
# by the standalone dictation tool (src/speech_to_speech/dictate/).
#
# This grants your user account access to /dev/uinput (system-wide synthetic
# input capability -- see README "Risks" section) via a dedicated group.
#
# Note: the Ubuntu-packaged ydotool (0.1.8, jammy) is an older build that
# only ships the `ydotool` client, no `ydotoold` daemon -- it talks to
# /dev/uinput directly, so no daemon/service is needed here.
# Requires sudo. Review before running.
set -euo pipefail

GROUP=ydotool
RULES_FILE=/etc/udev/rules.d/99-ydotool.rules

echo "== Installing ydotool =="
sudo apt-get install -y ydotool

echo "== Creating '${GROUP}' group and adding $(whoami) to it =="
sudo groupadd -f "${GROUP}"
sudo usermod -aG "${GROUP}" "$(whoami)"

echo "== Writing udev rule granting ${GROUP} access to /dev/uinput =="
echo 'KERNEL=="uinput", GROUP="'"${GROUP}"'", MODE="0660"' | sudo tee "${RULES_FILE}" > /dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger

cat <<'EOF'

Done. IMPORTANT: your *GNOME session* does not have the new group membership
yet -- processes spawned by an already-running gnome-shell (including your
custom-shortcut hotkey) won't pick it up until you fully log out and back in
(a new shell + `newgrp ydotool` is enough to test from a terminal, but not
enough for the GNOME hotkey path). After logging back in, verify with:

    groups | grep ydotool
    ydotool type -- "hello world"    # should type into your focused window

EOF
