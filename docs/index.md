# Speech-to-Speech

Persönlicher Sprachassistent: Push-to-toggle-Sprachdialog (Jabra-Headset oder
Hotkey), entkoppelte STT-/TTS-Daemons, austauschbare Agenten-Backends (Claude
Agent SDK, Pi Coding Agent) und mehrere Oberflächen (Web-Cockpit, Telegram).

Diese Seite ist der Einstiegspunkt in die Projekt-Dokumentation, aufgebaut aus
den Markdown-Dateien in `docs/`.

## Wo anfangen

- **[Feature-Übersicht](specification.md)** -- grober Überblick über alle
  Kernfeatures, Stand aktueller Code.
- **Feature-Spezifikationen** (vertiefen einzelne Features, lösen die
  Übersicht schrittweise ab):
  - [Kern-Dialogschleife](specs/core-dialog-loop.md) -- Zustandsmodell
    Idle/Processing/Responding, Ereignisse, Stop/Barge-in.
  - [Denkprozess-Kanal & Stop-Markierung](specs/thinking-channel-and-stop-marker.md)
- **[Architektur-Vorschlag](architecture-proposal.md)** und
  **[Telegram-Bot-Vorschlag](telegram-bot-proposal.md)** -- ursprüngliche
  Design-Dokumente.
- **[Backlog](backlog.md)** -- offene Ideen/Baustellen.
- **[macOS-Setup](macos-setup.md)** -- Plattform-spezifische Einrichtung.

!!! note "Status"
    Diese Doku-Seite ist ein erster Test-Aufbau, um die
    GitHub-Actions-Pipeline nach GitHub Pages zu verifizieren. Struktur und
    Inhalt werden noch verfeinert (siehe geplante Zustandsdiagramme in den
    Spezifikationen).
