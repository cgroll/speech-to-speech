# speech-to-speech auf macOS (Apple Silicon)

Die App wurde ursprünglich für Linux gebaut (systemd, evdev, Wayland/ydotool,
GNOME-Hotkeys). Auf macOS (M-Series) laufen dieselben Code-Pfade; plattform-
spezifisches Verhalten kommt über `sys.platform == "darwin"`-Zweige dazu. Dieses
Dokument beschreibt nur die macOS-Besonderheiten – der grundsätzliche Aufbau
steht in der `README.md` und `docs/architecture-proposal.md`.

Backend-Fokus auf diesem Rechner: **Claude** (Default in `agent_backend.py`).
Telegram, Pi-Agent und Gemini sind nicht Teil dieses Setups.

## Was anders ist als unter Linux

| Thema            | Linux                          | macOS                                           |
|------------------|--------------------------------|-------------------------------------------------|
| STT-Runtime      | `onnx_asr` (CPU, int8)         | `nano-parakeet` auf **MPS** (Apple-GPU)         |
| TTS-Runtime      | `faster-qwen3-tts` (CUDA)      | `faster-qwen3-tts` **GGML/Metal** (`backend="ggml"`) |
| Daemon-Lifecycle | `systemctl --user`             | `subprocess.Popen` / `pkill` (`daemon_launch.py`) |
| Diktat-Tippen    | `ydotool` (+ QWERTZ-Keymap)    | Clipboard + AppleScript `Cmd+V` (layout-unabhängig) |
| Feedback         | `canberra-gtk-play`/`notify-send` | `afplay` / `osascript`                       |
| Hotkey           | GNOME Custom Shortcut          | **Shortcuts.app** → „Run Shell Script"          |
| Jabra-Knopf      | `evdev`                        | noch nicht unterstützt (siehe unten)            |

`uv sync` zieht die richtigen Abhängigkeiten automatisch: die Linux-only-Pakete
(`evdev`, `onnx-asr`, `numba`, `llvmlite`) tragen `sys_platform == 'linux'`-Marker,
`nano-parakeet` ist `darwin`-only, und `faster-qwen3-tts[ggml]` bringt das
Metal-Backend (`qwentts-cpp-python`).

## Setup

### 1. Dependencies

```bash
uv sync
brew install ffmpeg   # für die 1.5×-Wiedergabe (atempo) in playback.py
```

Notausgang, falls ffmpeg fehlt: `TTS_PLAYBACK_SPEED=1.0` in `config.py` umgeht
ffmpeg (dann Wiedergabe in Originalgeschwindigkeit).

### 2. CA-Bundle für Hugging-Face-Downloads (Firmen-Proxy / Zscaler)

In Firmennetzen mit TLS-Interception (hier: E.ON Zscaler) präsentiert der Proxy
ein eigenes Root-CA, das zwar im macOS-Schlüsselbund liegt, aber **nicht** im
`certifi`-Bundle, das Pythons HTTPS nutzt. Ohne Gegenmaßnahme scheitern
HF-Downloads mit `CERTIFICATE_VERIFY_FAILED`. Ein kombiniertes Bundle (certifi +
Schlüsselbund-Roots) an einem festen Pfad behebt das:

```bash
mkdir -p ~/.config/speech-to-speech
BUNDLE=~/.config/speech-to-speech/ca-bundle.pem
cat "$(uv run python -c 'import certifi; print(certifi.where())')" > "$BUNDLE"
security find-certificate -a -p /Library/Keychains/System.keychain >> "$BUNDLE"
security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain >> "$BUNDLE"
```

`daemon_launch.py` setzt `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` automatisch auf
dieses Bundle, wenn die Datei existiert – die Daemons erben es also beim Start.
Für manuelle Aufrufe (z. B. ein Modell-Download von Hand) selbst exportieren:

```bash
export SSL_CERT_FILE=~/.config/speech-to-speech/ca-bundle.pem
export REQUESTS_CA_BUNDLE=~/.config/speech-to-speech/ca-bundle.pem
```

### 3. Modelle

- **STT (Parakeet)**: wird beim ersten Daemon-Start von Hugging Face geladen
  (`nvidia/parakeet-tdt-0.6b-v3`) und im HF-Cache (`~/.cache/huggingface`)
  abgelegt. Teilt sich den Cache mit dem Schwester-Repo `~/repos/parakeet-dictate`.
- **TTS (Qwen3-TTS GGUF)**: zwei Dateien aus `Serveurperso/Qwen3-TTS-GGUF` –
  `qwen-talker-0.6b-customvoice-BF16.gguf` und `qwen-tokenizer-12hz-BF16.gguf`.
  Die CustomVoice-Stimmen (inkl. „aiden") stecken im talker-GGUF; diese zwei
  Dateien sind alles, was das Metal-Backend braucht.

> **Achtung Zscaler + HF Xet:** Diese GGUF-Dateien liegen in Hugging Faces
> Xet-Speicher. Der Download läuft über vorsignierte CDN-URLs
> (`us.aws.cdn.hf.co/xet-bridge-us/...`), in die der Zscaler-SmartProxy einen
> Parameter `_sm_nck=1` einfügt – das bricht die Signatur, Antwort `403`. Das
> betrifft **jeden** CLI-Client (Python, `curl`, nativer Xet-Client) und ist
> kein Zertifikatsproblem mehr. Der **Browser** läuft dagegen durch (dort greift
> die interaktive Zscaler-Auth).

**Vorgehen (Browser-Download, einmalig):** `tts.py` lädt die GGUFs direkt aus
`~/.config/speech-to-speech/models/`, wenn sie dort liegen – komplett ohne
HF-Download (per `gguf_talker_path`/`gguf_codec_path`). Also:

1. Im Browser beide Dateien herunterladen (→ `~/Downloads`):
   - <https://huggingface.co/Serveurperso/Qwen3-TTS-GGUF/resolve/main/qwen-talker-0.6b-customvoice-BF16.gguf?download=true>
   - <https://huggingface.co/Serveurperso/Qwen3-TTS-GGUF/resolve/main/qwen-tokenizer-12hz-BF16.gguf?download=true>
2. In den Modell-Ordner verschieben:
   ```bash
   mkdir -p ~/.config/speech-to-speech/models
   mv ~/Downloads/qwen-talker-0.6b-customvoice-BF16.gguf \
      ~/Downloads/qwen-tokenizer-12hz-BF16.gguf \
      ~/.config/speech-to-speech/models/
   ```
3. `uv run qwen-tts enable` – lädt jetzt vom lokalen Pfad auf Metal.

Alternativer Ordner via `TTS_GGUF_DIR`-Env. Fehlen die Dateien, fällt `tts.py`
auf den (hier am Proxy scheiternden) HF-Pull zurück.

### 4. Daemons starten / stoppen

Wie unter Linux, nur ohne systemd – die CLIs starten/killen die Prozesse direkt:

```bash
uv run parakeet-dictate enable     # STT-Daemon (lädt Modell auf MPS)
uv run qwen-tts enable             # TTS-Daemon (lädt Modell auf Metal)
uv run parakeet-dictate status     # running, state=idle
uv run parakeet-dictate disable
uv run qwen-tts disable
```

Oder in einem Rutsch inklusive App (wartet, bis beide Sockets antworten):

```bash
uv run speech-to-speech-start
```

Hinweis: Es gibt auf macOS keinen Supervisor. Stürzt ein Daemon ab, bleibt er
unten, bis er neu gestartet wird (Cockpit-Restart, `daemon_control`, oder erneut
`enable`).

### 5. Hotkeys via Shortcuts.app

Die physische Jabra-Taste wird auf macOS noch nicht gelesen (siehe unten). Bis
dahin: in **Shortcuts.app** je einen Kurzbefehl „Run Shell Script" anlegen und
auf die absoluten venv-Pfade binden (Projekt-venv: `…/speech-to-speech/.venv`):

- **Sprach-Dialog** (Turn starten/stoppen): `…/.venv/bin/speech-to-speech-toggle`
  – z. B. auf `Ctrl+Opt+D` legen. Setzt eine laufende `speech-to-speech`-App voraus.
- **Systemweites Diktieren**: `…/.venv/bin/parakeet-dictate toggle`
  – z. B. auf `Ctrl+Opt+F`. Setzt einen laufenden STT-Daemon voraus.

### 6. System-Freigaben (beim ersten Mal)

- **Mikrofon**: beim ersten Aufnahme-Start fragt macOS die Freigabe für das
  Terminal (bzw. die startende App) ab.
- **Accessibility**: der Diktat-Paste-Pfad (AppleScript `Cmd+V`) braucht die
  Freigabe unter *Systemeinstellungen → Datenschutz & Sicherheit →
  Bedienungshilfen*. Fehlt sie, meldet `typing_backend.py` einen klaren Hinweis.

## Später: physische Jabra-Taste

macOS hat kein `evdev`. Ein künftiger darwin-Listener kann den HID-Knopf über
IOKit/`hidapi` oder Karabiner-Elements abgreifen und auf
`speech-to-speech-toggle` mappen (braucht „Input Monitoring"-Freigabe).
`input_button.py` bleibt Linux-only; der Shortcuts-Hotkey deckt das Auslösen bis
dahin ab.
