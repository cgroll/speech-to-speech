#!/usr/bin/env bash
# One-time system setup for reading the Jabra Link 390's button as an input
# device (/dev/input/eventN, owned root:input, mode 660 by default).
#
# Rather than adding your user to the system-wide 'input' group (which would
# grant read access to *every* input device on this machine, keyboards and
# mice included -- a real keylogging capability), this creates a dedicated
# udev rule scoped to exactly this device's USB vendor:product ID
# (0b0e:2e57), in its own group. Same pattern as parakeet-dictate's
# scripts/setup_ydotool.sh (dedicated 'ydotool' group for /dev/uinput).
#
# Requires sudo. Review before running.
set -euo pipefail

GROUP=jabra-input
RULES_FILE=/etc/udev/rules.d/99-jabra-input.rules
VENDOR_ID=0b0e
PRODUCT_ID=2e57

echo "== Creating '${GROUP}' group and adding $(whoami) to it =="
sudo groupadd -f "${GROUP}"
sudo usermod -aG "${GROUP}" "$(whoami)"

echo "== Writing udev rule granting ${GROUP} access to the Jabra Link 390 input device =="
echo 'SUBSYSTEM=="input", ATTRS{idVendor}=="'"${VENDOR_ID}"'", ATTRS{idProduct}=="'"${PRODUCT_ID}"'", GROUP="'"${GROUP}"'", MODE="0660"' | sudo tee "${RULES_FILE}" > /dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger

cat <<'EOF'

Done. IMPORTANT: your current login session does not have the new group
membership yet -- fully log out and back in (a new shell + `newgrp
jabra-input` is enough to test from a terminal). After logging back in,
verify with:

    groups | grep jabra-input
    ls -la /dev/input/by-id/ | grep -i jabra   # or check event node group via:
    cat /proc/bus/input/devices | grep -A5 "Jabra Link 390"

Then run `uv run python scripts/identify_jabra_key.py` to find the keycode
sent by the button you want to use.

EOF
