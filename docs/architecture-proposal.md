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

1. ~~Entscheidung Repo-Zuschnitt und Reihenfolge (siehe offene Fragen).~~
   Erledigt am 2026-08-12, abweichend von der ursprünglich geplanten
   Reihenfolge unten (vorgezogen, weil konkret gebraucht): Repo-Zuschnitt
   ist **kein neues `voice-daemons`-Repo**, sondern das komplette
   `parakeet-dictate`-Projekt wandert nach `speech-to-speech` (als
   `src/speech_to_speech/dictate/`) -- Wunsch war, künftig nur noch ein
   Repo zu pflegen und `parakeet-dictate` irgendwann löschen zu können.
2. ~~`stt-daemon` aus `parakeet-dictate/src/parakeet_dictate/daemon.py` +
   `audio.py` extrahieren~~ -- stattdessen 1:1 nach
   `speech_to_speech/dictate/daemon.py` + `audio.py` verschoben (Namespace
   angepasst, Modellname jetzt aus `config.STT_MODEL_NAME` statt eigener
   Konstante). Protokoll unverändert (`toggle_record` tippt weiterhin selbst
   via `ydotool` -- die geplante Reduktion auf reines
   Transkript-Ergebnis ohne `type_text` im Daemon ist **noch offen**, siehe
   Punkt 4.
3. ~~`parakeet-dictate` auf Client umbauen~~ -- CLI (`dictate/cli.py`), Daemon,
   `typing_backend.py`, `keymap.py`, `feedback.py`, systemd-Unit und
   `setup_ydotool.sh` sind jetzt Teil von `speech-to-speech`
   (`project.scripts`: `parakeet-dictate`, `parakeet-dictate-daemon`). Das
   alte `parakeet-dictate`-Repo läuft unverändert weiter (eigener
   systemd-Service, eigener GNOME-Hotkey auf `<Super>d`) und wird erst nach
   Verifikation der neuen Variante manuell abgeschaltet/gelöscht.
4. ~~`speech-to-speech`: `stt.py`/Recorder-Teil von `audio_io.py` durch einen
   `dictate`-Daemon-Client ersetzen~~ Erledigt am 2026-08-12. Protokoll-
   Reduktion wie in Punkt 2 als offen vermerkt: statt den Daemon komplett auf
   reine Text-Rückgabe umzustellen (hätte `toggle_record`/`ydotool` für den
   Diktier-Hotkey gebrochen), bekam er ein zweites Kommandopaar,
   `start_recording`/`stop_recording` -- dieselbe Aufnahme-/
   Transkriptions-Mechanik wie `toggle_record`, aber ohne `type_text`, weil
   `speech-to-speech` den Text an Claude weiterreicht statt ihn zu tippen.
   Neuer dünner Client `speech_to_speech/stt_client.py`; `app.py` hält kein
   eigenes Parakeet-Modell und keinen eigenen `Recorder` mehr, `audio_io.py`
   ist jetzt reines Wiedergabe-Modul, `stt.py` entfernt. Die doppelte
   Speicherlast ist damit behoben -- Voraussetzung bleibt, dass der Daemon
   vorher läuft (`parakeet-dictate enable`); `App.load()` bricht mit klarer
   Fehlermeldung ab, wenn er nicht erreichbar ist, statt ihn selbst zu
   starten (bewusst kein Auto-Start, wie beim Diktier-Pfad). Mic-Exklusivität
   (offene Frage oben) ist damit real: Diktieren und Sprach-Dialog können
   nicht mehr gleichzeitig laufen, weil beide denselben Daemon-Zustand
   beanspruchen.
5. ~~`tts-daemon` analog aus `tts.py` + Playback-Teil von `audio_io.py`
   extrahieren.~~ Erledigt am 2026-08-12. Namensgebung analog
   `parakeet-dictate`: CLI/systemd-Service/Socket heißen `qwen-tts`
   (Modellname statt Funktionsname). Neues Paket
   `speech_to_speech/tts_daemon/` (Struktur wie `dictate/`): `tts.py`
   (Modell-Klasse, 1:1 aus dem alten Top-Level-`tts.py`), `playback.py`
   (`play_audio_streaming`, 1:1 aus `audio_io.py` -- die dort schon
   unbenutzte blockierende `play_audio`/`_time_stretch`-Variante wurde beim
   Verschieben als toter Code gestrichen), `protocol.py` (Kopie von
   `dictate/protocol.py` mit eigenem Socket-Namen), `daemon.py`, `cli.py`
   (nur `enable`/`disable`/`status`, kein `toggle` -- es gibt keinen
   Hotkey-Nutzer dieses Daemons).

   Eine strukturelle Abweichung vom STT-Daemon war nötig: `speak` läuft
   serverseitig ab (blockiert für die gesamte Sprechdauer, siehe Schritt 6
   unten), und ein Barge-in muss ein laufendes `speak` per `stop` von einer
   *zweiten*, gleichzeitig offenen Verbindung abbrechen können -- der
   STT-Daemon bearbeitet dagegen eine Verbindung nach der anderen
   synchron, weil alle seine Kommandos schnell zurückkehren. Der
   TTS-Daemon startet deshalb pro Verbindung einen eigenen Thread. Die
   Abbruchlogik selbst ist unverändert die schon vorhandene
   `stop_event`-Unterstützung in `play_audio_streaming` -- wie im Abschnitt
   "Bezug zur Daemon-Architektur" unten vorausgesehen, ließ sie sich nahezu
   unverändert übernehmen, nur hält jetzt der Daemon das Event statt
   `app.py`. Die "Zeit bis erste Sprachausgabe" (`first_chunk_s`) muss
   dafür nicht live während der Wiedergabe zurückgemeldet werden -- der
   Cockpit-Stat wird ohnehin erst nach dem Turn gelesen, daher reicht ein
   normales Request/Response mit dem Wert in der finalen `speak`-Antwort,
   kein Streaming-Protokoll.

   **Nachtrag (noch am 2026-08-12, beim Live-Test entdeckt):** `stop_event`
   wird in `play_audio_streaming` nur *zwischen* Chunks geprüft, nie während
   eines laufenden Chunk-Aufrufs -- normalerweise unter einer Sekunde, aber
   ein Testsatz brachte den Daemon einmal für über vier Minuten in
   `state=speaking`, ohne dass `stop()` griff (das Problem gab es
   identisch schon im alten Ein-Prozess-Code, wurde dort aber nie sichtbar,
   weil `app.py`s eigener State sofort weiterschaltete). Im Daemon ist das
   folgenreicher: eine hängende Generierung blockiert den *gemeinsamen*
   Prozess für alle künftigen Anfragen (`busy: speaking`), nicht nur eine
   App-Instanz. Da ein blockierender GPU-Aufruf aus einem anderen Thread
   heraus nicht abbrechbar ist, bekam `_speak()` einen Watchdog: läuft die
   Wiedergabe länger als `_SPEAK_WATCHDOG_S` (90s, siehe `daemon.py`),
   beendet sich der Daemon-Prozess selbst (`os._exit`), und
   `systemd/qwen-tts.service`s `Restart=on-failure` startet ihn neu (~10s
   Modell-Ladezeit statt unbegrenztem Hängen). `tts_daemon/protocol.py`s
   `send_command` behandelt eine mitten in der Antwort abgebrochene
   Verbindung seither wie "Daemon nicht erreichbar" statt eine unbehandelte
   Exception zu werfen. Die Watchdog-Logik selbst ist isoliert getestet
   (Fake-Generator, gepatchtes kurzes Timeout); der ursprüngliche
   4-Minuten-Hänger ließ sich mit demselben Testsatz auf einem frisch
   neugestarteten Daemon nicht reproduzieren -- die genaue Ursache auf
   TTS-Bibliotheksebene bleibt offen, der Watchdog begrenzt aber in jedem
   Fall den Schaden, falls es wieder passiert.

   Bekannte, bewusst nicht behobene Lücke: `app.py` fängt in `_respond()`
   keine Exceptions von `stt_client`/`tts_client`-Aufrufen ab -- stirbt der
   Daemon-Aufruf mitten im Turn (z.B. durch den Watchdog-Neustart), bleibt
   auch `app.py`s eigener State auf `speaking`/`thinking` hängen, bis die
   App neu gestartet wird. Betrifft symmetrisch auch den STT-Pfad und
   existierte in der Form schon vorher; nicht Teil dieser Härtung.
6. ~~`speech-to-speech`: `tts.py`/Playback-Teil durch `tts-daemon`-Client
   ersetzen; `app.py` wird zum reinen Orchestrator.~~ Erledigt am
   2026-08-12. Neuer dünner Client `speech_to_speech/tts_client.py`
   (`ensure_available()`/`speak()`/`stop()`), analog `stt_client.py`. `App`
   hält kein `TextToSpeech`-Objekt mehr; `load()` prüft beide Daemons vorab
   (`stt_client.ensure_available()` und `tts_client.ensure_available()`),
   bricht mit klarer Fehlermeldung ab statt später beim ersten Knopfdruck
   zu hängen. Barge-in während `speaking` (drei Stellen: `on_toggle`,
   `submit_text`, `reset()`) ruft jetzt zusätzlich `tts_client.stop()` --
   vorher reichte dafür das lokal geteilte `threading.Event`, das ging mit
   dem Umzug der Wiedergabe in einen eigenen Prozess nicht mehr.
   `audio_io.py` und das Top-Level-`tts.py` sind komplett entfernt, ihr
   Inhalt lebt nur noch im `tts_daemon`-Paket. Damit ist auch die zweite
   Hälfte der doppelten GPU-Modelllast behoben (analog zur STT-Seite in
   Schritt 4) -- `app.py` selbst hält jetzt kein Modell mehr, weder STT
   noch TTS.

## Zusatz-Feature: Unterbrechbare Sprachausgabe (Barge-in)

Status: Umgesetzt (Commit "Add barge-in support to interrupt
thinking/speaking"). Festgehalten am 2026-08-12, im selben Kontext wie der
Daemon-Vorschlag oben entstanden.

### Problem

Der Toggle-Handler in `app.py` kennt aktuell nur zwei aktive Zustände
sinnvoll: `idle` (Knopfdruck startet Aufnahme) und `recording` (Knopfdruck
stoppt und löst LLM+TTS aus). Ein Knopfdruck während `thinking` oder
`speaking` wird komplett ignoriert ("Busy, ignoring toggle"). Der Nutzer
muss eine unpassende oder zu lange Antwort deshalb immer vollständig
anhören, bevor er neu sprechen kann.

### Vorschlag

Konsistentes Prinzip für alle Zustände: ein Knopfdruck gibt dem Nutzer
jederzeit die Kontrolle zurück, statt nur in `idle`/`recording` zu wirken.

- `speaking` -> Knopfdruck bricht die laufende Wiedergabe sofort ab und
  startet direkt eine neue Aufnahme (kein Umweg über `idle`).
- `thinking` -> Knopfdruck bricht den laufenden Claude-Query ab (der Claude
  Agent SDK Client bietet dafür bereits eine eingebaute `interrupt()`-Methode,
  siehe `ClaudeSDKClient.interrupt()` in `client.py`) und startet ebenfalls
  direkt eine neue Aufnahme.

Technisch:

- `audio_io.play_audio_streaming` bekommt ein `threading.Event` als
  Abbruch-Signal, das zwischen den einzelnen Audio-Chunks geprüft wird. Wird
  es gesetzt, wird der Output-Stream geschlossen und ein laufender
  ffmpeg-Zeitraffer-Prozess terminiert.
- Weil `tts.py`s `synthesize_streaming` ein Generator ist, der Chunks erst
  bei Bedarf von der GPU erzeugt, stoppt allein das Nicht-mehr-Abrufen aus
  der Konsum-Schleife automatisch auch die weitere Sprachsynthese -- kein
  separater Cancel-Pfad in `tts.py` nötig.
- `llm.py` bekommt eine `cancel()`-Methode, die über den bestehenden
  Event-Loop-Thread `ClaudeSDKClient.interrupt()` aufruft.
- Ein kurzer Sound-Hinweis beim Abbruch (Muster wie `feedback.py` in
  `parakeet-dictate`) bestätigt dem Nutzer akustisch, dass der Abbruch
  angekommen ist.

Bewusst nicht vorgesehen: automatische Sprachaktivitätserkennung während
der Wiedergabe (echtes "Reinreden" ohne Knopfdruck). Das würde ein offenes
Mikrofon parallel zur Lautsprecherausgabe erfordern, und ohne
Echo-Unterdrückung würde die eigene TTS-Ausgabe als Nutzer-Sprache
missinterpretiert -- deutlich höherer Aufwand, passt auch nicht zum
bestehenden Push-to-toggle-Muster.

### Bezug zur Daemon-Architektur

Der `tts-daemon` aus dem Vorschlag oben braucht von Anfang an ein
`stop`/`cancel`-Kommando im Protokoll, nicht erst nachträglich ergänzt --
sonst müsste das Protokoll später erneut angefasst werden. Die
Chunk-Abbruchlogik sollte deshalb so prozessunabhängig gebaut werden, dass
sie sich später fast unverändert in den Daemon übernehmen lässt.

## Zusatz-Feature: Web-Cockpit als drittes Steuer-Interface

Status: Umgesetzt (Chatverlauf, Unterbrechen-Button, Kennzahlen,
Neue-Session-Reset -- `src/speech_to_speech/cockpit.py`, läuft lokal auf
`http://127.0.0.1:7860`). Festgehalten am 2026-08-12. Offen bleibt nur der
mobile Zugriff, siehe Abschnitt unten.

### Motivation

Zusätzlich zu Jabra-Knopf und lokalem Hotkey eine kleine Weboberfläche als
drittes Trigger-Interface: schönere Anzeige des Chatverlaufs, ein
Unterbrechen-Button als Alternative zum physischen Knopf, Anzeige von
Kennzahlen wie Antwort-/Sprechdauer, und eine "Neue Session"-Funktion.

### Technischer Ansatz

Gradio statt Dash, weil `gr.Chatbot` fertige Chat-Bubble-Darstellung
mitbringt und Dash eher auf Analytics-Dashboards mit Graphen zugeschnitten
ist -- für eine Chat-Optik wäre mit Dash deutlich mehr Handarbeit nötig.

Die Gradio-App läuft im selben Prozess wie `app.py`, als weiterer
Hintergrund-Thread neben Jabra-Listener und Socket-Server (analog zu
`_run_jabra_listener`). Der Unterbrechen-Button ruft direkt dieselbe
`on_toggle`/Cancel-Logik der `App`-Instanz auf -- normaler Python-Aufruf,
kein neues Netzwerkprotokoll. Das bestehende Locking um die
Zustandsübergänge deckt automatisch auch Klicks aus der Weboberfläche ab.

Für Live-Anzeige von Zustand und Chatverlauf reicht Gradios eingebaute
Timer-Komponente (Polling alle paar hundert Millisekunden), echtes
Websocket-Pushing ist für eine Status-Cockpit-Anzeige nicht nötig -- die
eigentliche Sprachausgabe läuft ja weiterhin über die lokalen Lautsprecher.

Zwei Ergänzungen an `App` werden dafür ohnehin gebraucht (unabhängig vom
UI-Framework):

- Eine Liste, die den Chatverlauf strukturiert mitschreibt (aktuell nur im
  Log, nicht im State).
- Eine `reset()`-Funktion für eine neue Session (Claude-Client neu
  verbinden, Chatverlauf leeren).

Die Zeitmessungen für Antwort- und Sprechdauer existieren größtenteils
schon (`time.monotonic()` in `tts.py`/`audio_io.py`), müssten aber
zusätzlich in ein kleines Statistik-Objekt geschrieben werden statt nur
ins Log.

### Offene Frage: mobiler Zugriff (Handy)

Naheliegender Folgewunsch: dieselbe Oberfläche auch vom Handy aus nutzen.
Hier unterscheidet sich die Aufgabe grundlegend vom lokalen Cockpit, weil
das Handy kein direktes Mikrofon-/Lautsprecher-Passthrough zur laufenden
App hat -- Audio muss über das Netz geschickt und empfangen werden, und
zwar gestreamt (Chunk für Chunk), nicht als Datei nach Aufnahmeende, sonst
geht der bereits erarbeitete Time-to-first-audio-Vorteil der
Streaming-Wiedergabe verloren.

Zwei Optionen, noch nicht entschieden:

1. **Gradio-Cockpit auch im mobilen Browser öffnen.** Gradio unterstützt
   inzwischen streamendes Audio-Input/-Output (`gr.Audio(streaming=True)`),
   das im mobilen Safari/Chrome über die normale Mikrofon-Berechtigung
   funktioniert. Kein zweiter Code-Stack, schnellster Weg.
2. **Eigene Flutter-App.** Vorarbeit dazu existiert bereits im
   Nachbarprojekt `~/research/flutter_voice_stream` (dort allerdings mit
   Gemini + Geräte-eigenem STT/TTS, nicht mit dieser Pipeline -- kein
   direkt wiederverwendbarer Code, aber Erfahrung mit dem Tooling
   vorhanden). Nativeres Gefühl, Hintergrundbetrieb möglich, aber eine
   zweite UI, die bei jeder Cockpit-Änderung (Unterbrechen-Button,
   Chatverlauf, Statistiken) separat nachgezogen werden muss.

Tendenz: erst Option 1 versuchen, Flutter erst dann angehen, wenn konkrete
Grenzen des Browser-Wegs (Hintergrundbetrieb, Latenz, Bedienkomfort) im
echten Gebrauch tatsächlich stören, statt von Anfang an zwei Frontends zu
pflegen.

**Sicherheitsvoraussetzung, unabhängig von der gewählten Option:** Das
LLM-Backend läuft mit `permission_mode="bypassPermissions"` und vollem
Tool-Zugriff (Bash, Dateisystem, Websuche). Erreichbarkeit übers freie
Internet ohne Zugriffsschutz ist damit kein rein kosmetisches Risiko --
jede erreichte Stimme könnte beliebige Shell-Kommandos auslösen. Mobiler
Zugriff braucht deshalb in jedem Fall entweder ein privates VPN (z.B.
Tailscale) oder einen Tunnel mit Zugriffsschutz (z.B. Cloudflare Tunnel mit
Access-Regel) -- ein offener Port mit bloßer URL reicht nicht.

## Priorisierte Reihenfolge über alle drei Vorschläge

Festgehalten am 2026-08-12. Reihenfolge: zuerst Barge-in, dann Web-Cockpit,
Daemon-Aufspaltung zuletzt.

- **1. Barge-in.** Kleinster Aufwand (drei Dateien: `app.py`, `audio_io.py`,
  `llm.py`), sofort spürbarer Nutzen im Alltag, und harte Voraussetzung für
  Schritt 2 -- der Unterbrechen-Button im Cockpit soll dieselbe
  Abbruch-Logik aufrufen, die es ohne diesen Schritt noch gar nicht gibt.
- **2. Web-Cockpit.** Läuft im selben Prozess, ruft bestehende (und die neu
  aus Schritt 1 entstandenen) Funktionen direkt auf -- überschaubarer
  Aufwand, schnell sichtbarer Mehrwert (Chatverlauf, Kennzahlen,
  Session-Reset).
- **3. Daemon-Aufspaltung.** Größter Aufwand, mit noch offenen
  Grundsatzfragen (Repo-Zuschnitt, Protokoll-Details), und bringt kein
  neues Nutzer-Feature, sondern behebt vor allem doppeltes Modell-Laden und
  langsame Iteration bei Codeänderungen -- ein Investment in die eigene
  Entwicklungsgeschwindigkeit, kein User-Feature. Nebeneffekt der späten
  Reihenfolge: das Daemon-Protokoll (inkl. `stop`/`cancel`-Kommando für
  Barge-in) wird erst entworfen, wenn die tatsächlichen Anforderungen aus
  Schritt 1 und 2 bekannt sind, statt es vorab zu erraten. ~~Wird
  angegangen, sobald doppeltes Modell-Laden oder langsame Iteration im
  Alltag wirklich stören.~~ Umgesetzt am 2026-08-12 (siehe Schritte 5+6
  oben) -- `app.py` hält jetzt kein Modell mehr selbst, weder STT noch TTS.

## Nächste Schritte (Ergänzung, für die spätere Session)

7. ~~Abbruch-Event in `audio_io.play_audio_streaming` und `llm.cancel()`
   umsetzen, Zustandsmaschine in `app.py` um die Abbruch-Übergänge
   erweitern.~~ Erledigt.
8. ~~Chatverlauf-State und `reset()` in `App` ergänzen, dann Gradio-Cockpit
   als dritten Trigger-Thread aufsetzen.~~ Erledigt.
9. Entscheidung zu mobilem Zugriff treffen (Browser vs. Flutter), erst
   danach ggf. VPN/Tunnel-Absicherung einrichten. Weiterhin offen.
