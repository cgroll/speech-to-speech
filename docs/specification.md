# Specification (retroactive)

Diese Datei fasst die Kernfeatures des Projekts zusammen.

## 1. Hauptmerkmale

- **Saubere Zustandsmodelle**: Klare Trennung zwischen [Kern-Text-Dialog](specs/core-text-dialog-loop.md) und [Sprach-Erweiterung](specs/speech-extension.md).
- **STT + TTS**: Lokale Spracherkennung und Sprachausgabe über entkoppelte Daemons.
- **Mehrere Kanäle**: Lokales [Gradio-Cockpit](channels.md) und [Telegram-Bot](channels.md) für die Remote-Nutzung.
- **Steuerung des persönlichen Agenten**:
    - **Agenten-Wahl**: Wechsel zwischen verschiedenen Backends (z.B. Claude Code vs. Pi).
    - **Workspace-Scoping**: Auswahl des Arbeitsverzeichnisses (`root`), das die Agent-Konfiguration (`AGENTS.md`) und den Dateizugriff bestimmt.
    - **Session-Management**: Fortsetzen früherer Sessions über beide Backends hinweg.

## 2. Interaktionsmodell (Sprache & Text)

- **Eingabe-Modi:** Unterstützt Spracheingabe (Jabra/Hotkey) und Texteingabe (Cockpit/Telegram).
- **Steuerung der Sprachausgabe (TTS):**
    - **Sofortiger Stopp & Queue-Löschung:** Der Start einer Spracheingabe (Knopf drücken) oder das Abschicken einer Texteingabe (Enter/Button) bricht eine laufende Sprachausgabe sofort ab und löscht die gesamte Warteschlange für Sprachausgaben (**Speech-Queue**).
    - **Audio-Unterdrückung bei aktiver Eingabe:** Während eine Aufnahme läuft oder eine Nachricht in der Warteschlange auf Verarbeitung wartet, wird für neu eintreffende Ereignisse (z. B. fertige Hintergrund-Prozesse) kein Audio generiert. Diese erscheinen nur textlich im Verlauf.
    - **Keine Unterbrechung durch Tippen:** Das bloße Tippen im Text-Editor unterbricht die Sprachausgabe nicht.
- **Steuerung des Agenten (LLM):**
    - **Warteschlangen-Modell (Steering Messages):** Neue Eingaben (Sprache oder Text) unterbrechen den Denkprozess (Textgenerierung) des Agenten nicht hart. Stattdessen werden sie als Folge-Nachrichten eingereiht, die verarbeitet werden, sobald der Agent den aktuellen Turn (oder den nächsten Tool-Schritt) beendet hat.
    - **Text-Session-Konsistenz:** Der neue Input wird am Ende der Text-Historie eingereiht, nachdem alle bis dahin eingegangenen (auch asynchronen) Text-Outputs verarbeitet wurden. Die Audio-Ebene ist flüchtig (wird bei Interrupt gelöscht), die Text-Ebene permanent (wird erweitert).
- **Zustandsmaschine:** Das System basiert auf einer Kern-Text-Dialogschleife (`idle -> thinking -> responding`), die für den Sprachmodus um dedizierte Zustände für Aufnahme (`recording`) und Wiedergabe (`speaking`) erweitert wird. Details siehe [Kern-Text-Dialogschleife](specs/core-text-dialog-loop.md) und [Sprach-Erweiterung](specs/speech-extension.md).
- **Streaming:** Antworten werden satzweise synthetisiert und abgespielt -- die Wiedergabe beginnt nach dem ersten Satz.

## 2. Daemon-Architektur (STT/TTS entkoppelt von der App)

- Spracherkennung (Parakeet, CPU/MPS) und Sprachausgabe (Qwen3-TTS, GPU/
  Metal) laufen als eigene Hintergrunddienste mit eigenem Lebenszyklus
  (`enable`/`disable`/`status`), kein Auto-Start durch die App.
- Kommunikation über Unix-Sockets (`stt_client.py`/`tts_client.py` als
  dünne Clients).
- Watchdog im TTS-Daemon: erkennt hängende Chunk-Generierung und beendet den
  Prozess selbst, `Restart=on-failure` (systemd) bzw. manueller Neustart
  (macOS) holt ihn zurück.
- Graceful Degradation: fehlt der Jabra (Button oder Ausgabegerät), läuft die
  App weiter (Hotkey-Pfad bzw. Fallback auf Standard-Ausgabegerät).

## 3. Mehrfach-Oberflächen

- **Web-Cockpit** (Gradio, `cockpit.py`): Chatverlauf, Unterbrechen-Button,
  Kennzahlen (Antwort-/Sprechzeit), "Neue Session"-Reset, Auswahl früherer
  Sessions, Workspace-Auswahl (Textbox + Ordner-Browser), Mute-Checkbox für
  Sprachausgabe, Buttons zum Neustarten von STT-/TTS-Daemon. Standardmäßig
  nur lokal erreichbar (`127.0.0.1:7860`).
- **Telegram-Bot** (`telegram_bot/`): Text- und Sprachnachrichten von
  unterwegs, Long-Polling ohne offenen Port, Slash-Commands (`/new`,
  `/agent`, `/sessions`, `/voice`, `/workspace`, `/restart_stt`,
  `/restart_tts`, `/restart_bot`), bewusst text-only (kein TTS-Rückkanal).
  Pro Chat-ID eigene Konversation, nicht prozessübergreifend persistiert.

## 4. Mehrfach-Agenten-Backends

- Austauschbares LLM-/Agenten-Backend: Claude Agent SDK oder Pi Coding
  Agent, gemeinsames Interface (`send()`/`cancel()`/`close()`,
  `agent_backend.py`).
- Wahl per Start-Flag (`--agent`) und pro neuer Session (Cockpit/Telegram).
- Workspace-Scoping pro Session (`cwd` fürs jeweilige Backend), weiche
  System-Prompt-Instruktion zusätzlich zur harten `cwd`-Durchsetzung.
- Frühere Sessions wieder aufnehmbar (`sessions.py`, backend-spezifisches
  Listing/Parsing, gemeinsame Merge-Schicht mit Backend-Tag pro Eintrag).

## 5. Agenten-Werkzeuge/Skills

- `show_image`-Tool: Agent kann Bilder an die aktuelle Oberfläche schicken
  (Cockpit inline, Telegram `send_photo`) -- aktuell Claude-only.
- `reminder`-Skill: zeitgesteuerte Telegram-Erinnerungen über Google Cloud
  Tasks, mit sofortiger Erfolgsbestätigung unabhängig vom LLM-Antworttext
  und einem `--list`-Modus für ausstehende Erinnerungen.
- `bookmark`-Skill: per Telegram geteilte URLs werden automatisch (ohne
  Rückfrage) mit Titel, Tags und Kurzbeschreibung als Obsidian-Notiz
  abgelegt.

## 6. Eigenständiger Diktier-Modus (`parakeet-dictate`)

- Hotkey drücken, sprechen, nochmal drücken -- Text wird direkt ins
  fokussierte Textfeld getippt (`ydotool`/macOS-Zwischenablage), unabhängig
  vom Sprachdialog.
- DE/QWERTZ-Layout-Fixup (Linux), Sound-/Notify-Feedback.
- Mic-Exklusivität: Diktieren und Sprachdialog teilen sich Mikrofon/Daemon,
  können nicht gleichzeitig laufen (zweiter Aufruf bekommt `busy`).

## 7. Robustheit/Betrieb

- Aktivitätsbasierter Timeout für hängende Agenten-Antworten
  (`LlmTimeoutError`), sauberer Rücksprung nach `idle` statt stillem Hängen.
- Manueller Daemon-Neustart aus App/Cockpit/Telegram heraus
  (`daemon_control.py`), bricht laufende Aufnahme/Wiedergabe vorher sauber
  ab.
- Plattform-Unterschiede Linux (systemd, `evdev`, GNOME-Shortcuts,
  `ydotool`) vs. macOS Apple Silicon (MPS/Metal, kein systemd, eigener
  Prozess-Lifecycle über `daemon_launch.py`, Shortcuts.app-Hotkeys,
  Zscaler-Zertifikatshinweise).
- Bekannte Grenzen: kein Review/Edit-Schritt vor dem LLM-Versand,
  hartkodierter Jabra-Keycode, TTS-Rohgeschwindigkeit ca. 0,7x Echtzeit ohne
  flash-attn.
