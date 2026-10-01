"""macOS daemon lifecycle (start/stop/restart) via subprocess -- the Popen/
pkill counterpart to Linux's `systemctl --user`. Shared by the per-daemon CLIs
(dictate, tts, telegram) and daemon_control.py so the launch/teardown logic
lives in one place and the three stay consistent.

A daemon is launched by its console-script entry point (pyproject
[project.scripts]), resolved next to the *current* interpreter -- i.e. the same
venv that's running this code -- so it works whether invoked via `uv run` or the
installed script, with no PATH assumptions. pkill matches that same entry-point
name on the command line; the `-daemon` suffix keeps it from matching the bare
CLI (e.g. `parakeet-dictate toggle`).

Unlike systemd there's no supervisor here: a crashed daemon stays down until
restarted (cockpit/daemon_control or a fresh `enable`). See the macOS port plan,
risk 5.
"""

import os
import subprocess
import sys

# Corporate TLS-interception (E.ON Zscaler) presents a root CA that lives in the
# macOS keychain but not in certifi's bundle, so Python HTTPS to Hugging Face
# fails cert validation. The daemons download/refresh models from HF, so point
# their SSL stack at a combined bundle (certifi + macOS keychain roots) when one
# has been generated. See README (macOS setup) for how to build it. Absent the
# file we set nothing and fall back to certifi -- fine on networks without the
# proxy, and for runs that only touch the already-cached models.
CA_BUNDLE_PATH = os.path.expanduser("~/.config/speech-to-speech/ca-bundle.pem")


def _executable(entry_point: str) -> str:
    return os.path.join(os.path.dirname(sys.executable), entry_point)


def _env() -> dict[str, str]:
    env = os.environ.copy()
    if os.path.exists(CA_BUNDLE_PATH):
        env.setdefault("SSL_CERT_FILE", CA_BUNDLE_PATH)
        env.setdefault("REQUESTS_CA_BUNDLE", CA_BUNDLE_PATH)
    return env


def start(entry_point: str, socket_path: str) -> None:
    # Remove a stale socket so the fresh daemon can bind cleanly. A daemon
    # that's actually still alive is holding its own socket and won't be here
    # (callers gate on that); a leftover file from a crash would otherwise make
    # bind() fail with EADDRINUSE.
    if os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError:
            pass
    subprocess.Popen(
        [_executable(entry_point)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=_env(),
    )


def stop(entry_point: str, socket_path: str) -> None:
    subprocess.run(["pkill", "-f", entry_point], check=False)
    if os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError:
            pass


def restart(entry_point: str, socket_path: str) -> None:
    stop(entry_point, socket_path)
    start(entry_point, socket_path)
