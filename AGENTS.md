# AGENTS.md

Kontext für Agenten, die an diesem Projekt arbeiten.

## Worum es geht

`speech-to-speech` ist ein Proof-of-Concept für ein Sprach-Interface: Sprache
rein, Sprache raus. Ein Knopf am Jabra-Headset (oder eine lokale
Tastenkombination) startet/stoppt eine Aufnahme, das Gesagte wird lokal per
Parakeet (ONNX, CPU) transkribiert, an ein LLM-Backend geschickt (Claude Agent
SDK oder Pi Coding Agent, austauschbar über `--agent`/Cockpit-Auswahl, siehe
`src/speech_to_speech/agent_backend.py`) und die Antwort wird lokal per
Qwen3-TTS (GPU, fester Sprecher "aiden") vorgelesen. Ein einziger Prozess
hält STT- und TTS-Modell warm und läuft im Vordergrund
(`uv run speech-to-speech`).

Details zu Setup, Architektur der einzelnen Module (`audio_io.py`, `stt.py`,
`tts.py`, `llm.py`, `app.py`, `input_button.py`, `toggle_socket.py`) und
bekannten Grenzen stehen in `README.md` -- dort zuerst nachsehen, bevor man
sich durch den Code sucht.

## Wo Roadmap / Backlog / offene Themen stehen

Kleinere offene Punkte stehen gesammelt in `docs/backlog.md`. Größere, noch
nicht umgesetzte Architektur-Vorschläge und offene Fragen werden stattdessen
als eigene Markdown-Dateien unter `docs/` festgehalten, mit Status-Zeile am
Anfang (z.B. "Diskussionsstand, noch nicht umgesetzt") und einem Abschnitt
"Nächste Schritte" am Ende. Aktuell:

- `docs/architecture-proposal.md` -- Vorschlag, STT und TTS als geteilte
  Daemons zu betreiben (analog zum Schwesterprojekt `parakeet-dictate`),
  statt sie fest in diesen monolithischen Prozess eingebaut zu lassen.

Bevor man an Architektur-Änderungen arbeitet: `docs/backlog.md` und `docs/`
auf vorhandene Vorschläge/Entscheidungen prüfen, statt sie doppelt zu
treffen -- z.B. hält `docs/backlog.md`s Abschnitt "Mehrere Agent-Backends"
die noch offene Workspace-Eingrenzungsfrage für beide Backends fest.

## Verwandte Projekte

- `parakeet-dictate` (Nachbarordner unter `~/research/`) -- eigenständiges
  Diktier-Tool, lädt ebenfalls Parakeet-STT und dient hier als
  Referenzmuster für Daemon/CLI-Struktur (siehe architecture-proposal.md).
- `voice-agent-speech-to-speech-hf` -- externer HuggingFace-Referenz-Checkout
  für eine alternative VAD/STT/LLM/TTS-Pipeline-Architektur.
