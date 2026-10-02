# Speech-to-Speech

Persönlicher Sprachassistent: Push-to-toggle-Sprachdialog (Jabra-Headset oder
Hotkey), entkoppelte STT-/TTS-Daemons, austauschbare Agenten-Backends (Claude
Agent SDK, Pi Coding Agent) und mehrere Oberflächen (Web-Cockpit, Telegram).

Diese Seite ist der Einstiegspunkt in die Projekt-Dokumentation, aufgebaut aus
den Markdown-Dateien in `docs/`.

## Hauptmerkmale

- **Saubere Zustandsmodelle**: Klare Trennung zwischen [Text-Dialog](specs/core-text-dialog-loop.md) und [Sprach-Erweiterung](specs/speech-extension.md).
- **STT + TTS**: Lokale Spracherkennung und Sprachausgabe über entkoppelte Daemons.
- **Mehrere Kanäle**: Lokales [Gradio-Cockpit](channels.md) und [Telegram-Bot](channels.md) für die Remote-Nutzung.
- **Steuerung des persönlichen Agenten**:
    - **Agenten-Wahl**: Wechsel zwischen verschiedenen Backends (z.B. Claude Code vs. Pi).
    - **Workspace-Scoping**: Auswahl des Arbeitsverzeichnisses (`root`), das die Agent-Konfiguration (`AGENTS.md`) und den Dateizugriff bestimmt.
    - **Session-Management**: Fortsetzen früherer Sessions über beide Backends hinweg.

## Dokumentation

- **[Feature-Übersicht](specification.md)** -- Detaillierter Überblick über alle Kernfeatures.
- **[Kanäle und Output-Steuerung](channels.md)** -- Gradio vs. Telegram, Metadaten-Filterung und Input-Separation.
- **Feature-Spezifikationen**:
  - [Kern-Text-Dialogschleife](specs/core-text-dialog-loop.md) -- Grundmodell (Idle/Thinking/Responding) und User-Input-Queue.
  - [Sprach-Erweiterung (STT/TTS)](specs/speech-extension.md) -- Aufnahme-/Sprech-Zustände, Audio-Unterdrückung und Barge-in.
  - [Denkprozess-Kanal & Stop-Markierung](specs/thinking-channel-and-stop-marker.md)
- **Architektur & Design**:
  - [Architektur-Vorschlag](architecture-proposal.md) -- Ursprüngliches Design-Dokument.
  - [Telegram-Bot-Vorschlag](telegram-bot-proposal.md)
  - [macOS-Setup](macos-setup.md) -- Plattform-spezifische Einrichtung.
- **[Backlog](backlog.md)** -- Offene Ideen und Baustellen.

!!! note "Status"
    Diese Doku-Seite ist ein erster Test-Aufbau, um die
    GitHub-Actions-Pipeline nach GitHub Pages zu verifizieren. Struktur und
    Inhalt werden noch verfeinert (siehe geplante Zustandsdiagramme in den
    Spezifikationen).
