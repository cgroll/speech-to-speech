# Architektur-Vorschlag: STT/TTS als geteilte Daemons

Status: Diskussionsstand, noch nicht umgesetzt. Festgehalten am 2026-08-12,
um es in einer späteren Session anzugehen. Ausgangspunkt war die Frage, ob
sich STT und TTS als eigenständige Hintergrunddienste betreiben lassen,
ähnlich zu `parakeet-dictate`, statt fest in `speech-to-speech` eingebaut zu
sein.

## Ist-Zustand

`speech-to-speech` und `parakeet-dictate` sind zwei getrennte Projekte, die
beide unabhängig voneinander ein eigenes Parakeet-STT-Modell laden:

- `speech-to-speech/src/speech_to_speech/audio_io.py` ist laut eigenem
  Docstring ein Fork von `parakeet-dictate/src/parakeet_dictate/audio.py`
  (Mikrofonaufnahme + VAD-Segmentierung, `webrtcvad`, Pause-Erkennung).
- Beide laden `nemo-parakeet-tdt-0.6b-v3` über `onnx_asr` separat.
- `parakeet-dictate` hat bereits das Zielmuster vorgebaut: ein Daemon
  (`daemon.py`) hält das Modell warm und beantwortet Kommandos über einen
  Unix-Socket (JSON: `{"cmd": "toggle_record"}` -> `{"ok": true, "state":
  ...}`), gesteuert über einen systemd-User-Service mit `enable`/`disable`/
  `status`/`toggle` als CLI (`cli.py`). Der Hotkey ruft nur den dünnen
  CLI-Client auf, nie den Daemon direkt.
- `speech-to-speech` dagegen ist heute monolithisch: ein einziger Prozess
  hält STT- und TTS-Modell selbst, verarbeitet Jabra-Knopf (evdev) und
  lokalen Hotkey (eigener Unix-Socket `toggle_socket.py`) direkt, und
  orchestriert Aufnahme -> Transkription -> Claude Agent SDK -> TTS ->
  Wiedergabe komplett inline (`app.py`).
- Konsequenz: Laufen beide Projekte gleichzeitig, ist das STT-Modell doppelt
  im Speicher, und jede Codeänderung an `app.py`/`llm.py` in
  `speech-to-speech` erzwingt einen kompletten Neustart inklusive Laden
  beider (Multi-GB-)Modelle.
- Als Referenz von außen: `voice-agent-speech-to-speech-hf` (lokaler Checkout
  des HuggingFace-Projekts) bestätigt die Grundidee -- VAD/STT/LLM/TTS als
  austauschbare Pipeline-Stufen hinter einem Service --, dort aber als ein
  gemeinsamer Server mit OpenAI-Realtime-WebSocket-API, nicht als zwei
  getrennt ansprechbare Daemons.

## Vorschlag

Zwei neue, von den bestehenden Projekten unabhängige Daemons nach dem
etablierten `parakeet-dictate`-Muster (Unix-Socket, JSON-Kommandos,
systemd-User-Service, `enable`/`disable`/`status`):

**stt-daemon**
- Besitzt Mikrofon, VAD-Segmentierung und das Parakeet-Modell exklusiv.
- Schlankes Protokoll: `start_recording` / `stop_recording` ->
  Transkript-Text. Der Daemon entscheidet nicht, was mit dem Text passiert
  (kein Tippen, kein LLM-Aufruf eingebaut) -- das bleibt Sache des jeweiligen
  Clients.
- `parakeet-dictate` wird zum dünnen Client: ruft `stt-daemon`, tippt das
  Ergebnis per `ydotool` (inkl. Layout-Fixup `keymap.py`, Sound-Feedback
  `feedback.py`) -- verliert seine eigene Modell-/Recorder-Logik.
- `speech-to-speech` wird ebenfalls Client: `stt.py`/die
  Recorder-Verantwortung in `audio_io.py` entfallen, `app.py` ruft den
  Daemon für Aufnahme+Transkription.

**tts-daemon**
- Besitzt das Qwen3-TTS-Modell.
- Nimmt Text entgegen und spielt ihn selbst über die lokalen Lautsprecher ab
  (serverseitige Wiedergabe statt Audio-Rohdaten über den Socket zurück --
  auf einer Einzelmaschine deutlich einfacher als Streaming-Bytes durch
  einen Unix-Socket zu pipen).
- `speech-to-speech` wird Client: `tts.py`/die Playback-Verantwortung in
  `audio_io.py` entfallen, `app.py` schickt die Claude-Antwort nur noch an
  den Daemon.
- Aktuell nur ein Konsument (`speech-to-speech`), aber offen für spätere
  Tools (z.B. "lies mir das vor").

**Was bei den bestehenden Projekten bleibt**, weil es Client-/UX-spezifisch
ist, nicht Modell-Serving:
- Hotkey-/Knopf-Handling (Jabra-evdev, GNOME-Hotkey-Socket).
- Sound-Cues / `notify-send`-Feedback.
- Tastatur-Layout-Fixup, `ydotool`-Aufruf.
- Claude-Agent-Orchestrierung, Konversationshistorie.

## Erwarteter Nutzen

- Kein doppelt geladenes STT-Modell mehr, wenn beide Tools laufen.
- Schnelle Iteration an `app.py`/`llm.py`: Neustart des dünnen Clients lädt
  keine Multi-GB-Modelle mehr neu, nur der Daemon tut das (und läuft
  durchgehend).
- Klarere Trennung: Modell-Serving (Daemons) vs. Verhalten (Clients) --
  Diktier-Verhalten und Konversations-Verhalten bleiben bewusst getrennte,
  einfache Programme, nur die teure Infrastruktur wird geteilt.

## Offene Fragen / zu klärende Trade-offs

- **Repo-Zuschnitt:** Neues eigenständiges Repo für beide Daemons (z.B.
  `voice-daemons` mit zwei Unterprojekten), oder wandert die Daemon-Logik in
  eines der bestehenden Projekte (`parakeet-dictate` besitzt heute schon die
  sauberste Daemon/CLI-Struktur) und die anderen werden Clients davon?
- **Mic-Exklusivität:** Der `stt-daemon` besäße das Mikrofon exklusiv --
  Diktieren und Sprachgespräch könnten dann nicht mehr gleichzeitig laufen.
  Realistisch unproblematisch (eine Person, ein Mund), aber explizit zu
  bestätigen.
- **Reihenfolge:** Erst `stt-daemon` (löst die tatsächliche Code-Duplizierung
  zwischen den zwei bestehenden Projekten) oder gleich beide zusammen?
- **Protokoll-Details:** Exaktes JSON-Schema für Start/Stop/Status,
  Fehlerfälle (Daemon nicht erreichbar, Modell noch am Laden,
  Recording-Race zwischen zwei Clients), Timeout-Verhalten -- analog zu
  `parakeet-dictate/src/parakeet_dictate/daemon.py` und `cli.py` als
  Vorlage.
- **systemd-Service-Namensgebung** und ob `enable`/`disable` weiterhin
  bewusst nicht auto-startend bleiben (wie bei `parakeet-dictate` heute).

## Nächste Schritte (für die spätere Session)

1. Entscheidung Repo-Zuschnitt und Reihenfolge (siehe offene Fragen).
2. `stt-daemon` aus `parakeet-dictate/src/parakeet_dictate/daemon.py` +
   `audio.py` extrahieren, Protokoll auf reines Transkript-Ergebnis
   reduzieren (kein `type_text` im Daemon).
3. `parakeet-dictate` auf Client umbauen, gegen `stt-daemon` testen.
4. `speech-to-speech`: `stt.py`/Recorder-Teil von `audio_io.py` durch
   `stt-daemon`-Client ersetzen.
5. `tts-daemon` analog aus `speech-to-speech/src/speech_to_speech/tts.py` +
   Playback-Teil von `audio_io.py` extrahieren.
6. `speech-to-speech`: `tts.py`/Playback-Teil durch `tts-daemon`-Client
   ersetzen; `app.py` wird zum reinen Orchestrator (Toggle -> stt-daemon ->
   Claude -> tts-daemon).
