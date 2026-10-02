# speech-to-speech

Proof of concept: Sprache rein, Sprache raus. Ein Knopf am Jabra-Headset
(oder wahlweise eine lokale Tastenkombination) startet/stoppt die Aufnahme
(push-to-toggle, wie bei `parakeet-dictate`), das Gesagte wird lokal
transkribiert (Parakeet/CPU), an Gemini geschickt, und die Antwort wird
lokal per Qwen3-TTS (GPU, Stimme "aiden") vorgelesen.

Die App selbst hält kein Modell mehr warm -- STT (Parakeet) und TTS
(Qwen3-TTS) laufen beide in eigenen Hintergrund-Daemons, die App spricht sie
nur noch über ihre jeweiligen Unix-Sockets an (`stt_client.py`,
`tts_client.py`; siehe Abschnitte "Eigenständige Diktier-Funktion" und
"TTS-Daemon" unten sowie `docs/architecture-proposal.md`). Beide Daemons
müssen vorher gestartet sein (`parakeet-dictate enable` und `qwen-tts
enable`) -- kein Auto-Start durch die App.

Toggle-Auslöser kommen aus zwei Quellen: `evdev` liest den Jabra-Knopf
direkt, und ein schlanker Unix-Socket (`toggle_socket.py`) nimmt Kommandos
von einem optionalen lokalen Hotkey entgegen (siehe Setup Schritt 5). Fehlt
der Jabra (nicht angeschlossen), läuft die App trotzdem weiter -- nur über
den Socket-Pfad.

> **macOS (Apple Silicon):** Dieses Setup-Kapitel beschreibt Linux. Für den Mac
> (STT auf MPS, TTS auf Metal, Daemons ohne systemd, Shortcuts-Hotkeys, Firmen-
> Proxy/Zscaler-Hinweise) siehe **[docs/macos-setup.md](docs/macos-setup.md)**.

## Setup

### 1. Dependencies installieren

```bash
uv sync
```

### 2. Zugriff auf den Jabra-Knopf freischalten

Der Jabra Link 390 meldet sich als Eingabegerät
(`/dev/input/eventN`, `root:input`, Mode 660) -- ohne Freischaltung kann der
Prozess die Knopfdrücke nicht lesen. Das folgende Skript legt eine eigene
Gruppe an und eine udev-Regel, die **nur** dieses eine Gerät freigibt
(nicht die pauschale `input`-Gruppe, die Zugriff auf alle Eingabegeräte
gäbe):

```bash
./scripts/setup_jabra_input.sh
```

Danach **einmal komplett aus- und wieder einloggen** (neue Gruppenmitgliedschaft
wird sonst nicht wirksam).

### 3. Keycode des gewünschten Knopfs herausfinden

```bash
uv run python scripts/identify_jabra_key.py
```

Knopf am Jabra-Puck drücken, ausgegebenen Keycode (z.B. `KEY_PLAYPAUSE`)
notieren und in `src/speech_to_speech/config.py` bei `JABRA_TOGGLE_KEY`
eintragen.

### 4. Google-Auth für Gemini (Vertex AI)

Das Projekt `pi-agent-gemini` blockt per Org-Policy einfache
Gemini-Developer-API-Keys (`API_KEY_SERVICE_BLOCKED`). Stattdessen läuft
der Zugriff über Vertex AI mit Application Default Credentials:

```bash
gcloud auth application-default login   # einmalig, öffnet Browser-Login
```

Projekt/Region sind in `config.py` auf `pi-agent-gemini` / `us-central1`
voreingestellt, per `.env` (siehe `.env.example`) überschreibbar.

### 5. (Optional) Lokale Taste statt/zusätzlich zum Jabra-Knopf

Für den Fall, dass du eh am Rechner sitzt: eine beliebige Taste (z.B. eine
Numpad-Taste) kann denselben Toggle auslösen, ohne dass die App Zugriff auf
den rohen Tastatur-Eventstream braucht -- GNOME liefert dabei nur genau die
eine gebundene Taste, kein Keylogging-Risiko wie beim direkten
`evdev`-Zugriff auf die Haupttastatur.

GNOME Settings -> Keyboard -> View and Customize Shortcuts -> Custom
Shortcuts -> Add:
- Name: `speech-to-speech toggle`
- Command: `/home/chris/research/speech-to-speech/.venv/bin/speech-to-speech-toggle`
- Shortcut: z.B. eine Numpad-Taste

Der Befehl verbindet sich mit dem Unix-Socket der laufenden App
(`$XDG_RUNTIME_DIR/speech-to-speech.sock`) und schickt `toggle` -- exakt
dasselbe wie ein Jabra-Knopfdruck. Ist die App nicht gestartet, meldet der
Befehl das auf stderr und tut sonst nichts.

## Nutzung

```bash
uv run speech-to-speech-start
```

Startet beide Daemons per `systemctl --user start` (idempotent -- ein No-op,
falls einer schon läuft), wartet bis beide tatsächlich über ihren Socket
erreichbar sind (nicht nur bis `systemctl start` zurückkehrt -- das
Modell-Laden dauert danach noch ein paar Sekunden bis über eine Minute),
und startet dann die App. Ein Befehl statt der sonst nötigen drei
(`parakeet-dictate enable`, `qwen-tts enable`, `speech-to-speech`).
Voraussetzung: die systemd-User-Services beider Daemons sind installiert
(siehe "Eigenständige Diktier-Funktion" und "TTS-Daemon" unten).

Alternativ, wenn du die Daemons unabhängig von der App steuern willst (z.B.
länger laufen lassen als die App selbst):

```bash
parakeet-dictate enable
qwen-tts enable
uv run speech-to-speech
```

`speech-to-speech` allein bricht mit einer klaren Fehlermeldung ab, statt
später beim ersten Knopfdruck zu hängen, falls einer der beiden Daemons
nicht läuft. Zeigt sonst "Ready.", dann: Knopf drücken (Jabra oder lokaler
Hotkey), sprechen, nochmal drücken -> Transkript + Antwort erscheinen im
Log, die Antwort wird gesprochen. Beenden mit Ctrl+C (die Daemons laufen
danach weiter, unabhängig von der App).

## Architektur

- `stt_client.py` -- dünner Client für den gemeinsamen STT-Daemon
  (`dictate/daemon.py`): `start_recording()`/`stop_recording()` über dessen
  Unix-Socket, kein eigenes Parakeet-Modell mehr im App-Prozess. Der Daemon
  muss vorher separat gestartet sein (`parakeet-dictate enable`) -- kein
  Auto-Start durch die App.
- `tts_client.py` -- dünner Client für den gemeinsamen TTS-Daemon
  (`tts_daemon/daemon.py`, siehe eigener Abschnitt unten):
  `speak()`/`stop()` über dessen Unix-Socket. Synthese *und* Wiedergabe
  laufen serverseitig im Daemon (fester Sprecher "aiden"), `speak()`
  blockiert entsprechend bis die Wiedergabe fertig ist oder per `stop()`
  abgebrochen wird. Der Daemon muss vorher separat gestartet sein
  (`qwen-tts enable`) -- kein Auto-Start durch die App.
- `llm.py` -- Gemini-Chat-Session mit Konversationshistorie über die
  Laufzeit der App. Bewusst eine einzelne kleine `send()`-Funktion -- das
  ist der Punkt, an dem später ein anderes Backend (z.B. Claude Code)
  eingehängt werden kann.
- `input_button.py` -- `evdev`-Listener für den Jabra-Knopf.
- `toggle_socket.py` -- Unix-Socket-Server/-Client für den optionalen
  lokalen Hotkey (GNOME-Shortcut -> `speech-to-speech-toggle` -> Socket).
- `cockpit.py` -- Gradio-Web-Cockpit, drittes Steuer-Interface neben Jabra
  und lokalem Hotkey: Chatverlauf, Unterbrechen-Button (ruft dieselbe
  `on_toggle()` wie die anderen zwei Trigger), Kennzahlen (Antwort-/
  Sprechzeit), "Neue Session"-Reset. Läuft als eigener Hintergrund-Thread im
  selben Prozess, standardmäßig nur auf `http://127.0.0.1:7860` erreichbar
  (siehe `docs/architecture-proposal.md` zum Sicherheitsgrund und zu
  offenem mobilem Zugriff).
- `dictate/` -- eigenständiger System-weiter Diktier-Modus (Daemon + CLI),
  siehe eigener Abschnitt unten. Unabhängig vom Rest der App, teilt sich nur
  das Parakeet-Modell.
- `tts_daemon/` -- gemeinsamer TTS-Daemon (Daemon + CLI), siehe eigener
  Abschnitt unten. Einziger Ort, an dem das Qwen3-TTS-Modell lädt und
  Wiedergabe stattfindet.
- `telegram_bot/` -- Telegram-Bot-Daemon (Daemon + CLI), siehe eigener
  Abschnitt unten. Eigenständiger Prozess, nutzt denselben STT-Daemon
  (`stt_client.transcribe()`) und dieselbe Agent-Anbindung
  (`agent_backend.py`) wie die App, aber komplett unabhängig von `app.py`
  selbst (kein TTS, kein Jabra/Hotkey-Bezug).
- `app.py` -- State Machine: `idle -> recording -> thinking -> speaking -> idle`,
  angestoßen von beiden Toggle-Quellen (Jabra-Thread + Socket-Thread), gegen
  Races per Lock beim State-Übergang abgesichert.
  Die komplette Gemini-Antwort wird satzweise synthetisiert und abgespielt
  (`_speak_streaming`): ein Producer-Thread generiert Satz N+1, während der
  Hauptthread Satz N abspielt -- Wiedergabe startet also nach dem ersten
  Satz, nicht erst nach der ganzen (oft längeren) Antwort. Gleiches
  Producer/Consumer-Muster wie `_consume_segments` bei der Aufnahme, nur mit
  vertauschter Richtung (Generierung -> Wiedergabe statt Mikro ->
  Transkription).

## Eigenständige Diktier-Funktion (`parakeet-dictate`)

Zusätzlich zum Sprach-Dialog enthält dieses Projekt einen eigenständigen
System-weiten Diktier-Modus (`src/speech_to_speech/dictate/`, vormals das
getrennte `parakeet-dictate`-Repo): Hotkey drücken, sprechen, nochmal
drücken -- der transkribierte Text wird direkt in das gerade fokussierte
Textfeld getippt, unabhängig vom Gemini/Claude-Dialog oben. Der Daemon dieses
Diktier-Modus ist inzwischen auch der einzige Ort, an dem das Parakeet-Modell
lädt (`STT_MODEL_NAME` in `config.py`) -- der Sprach-Dialog (`app.py`) ist
über `stt_client.py` nur noch Client desselben Daemons, kein zweites
Modell-/Mikro-Handling mehr im App-Prozess (siehe
`docs/architecture-proposal.md`, "Daemon-Aufspaltung").

- Ein Daemon (`dictate/daemon.py`) lädt Parakeet und lauscht auf einem
  eigenen Unix-Socket (`$XDG_RUNTIME_DIR/parakeet-dictate.sock`) auf
  Toggle-/Status-Kommandos -- läuft nur, wenn explizit gestartet
  (`parakeet-dictate enable`/`disable`), kein Auto-Start.
- Der Hotkey ruft nur den dünnen CLI-Client (`dictate/cli.py`) auf:
  `parakeet-dictate toggle`. Der getippte Text kommt über `toggle_record`
  zurück.
- Der Sprach-Dialog (`stt_client.py`) nutzt stattdessen die "stillen"
  Kommandos `start_recording`/`stop_recording` desselben Daemons -- gleiche
  Aufnahme-/Transkriptions-Mechanik, aber ohne `ydotool`-Tippen, da der Text
  hier an Claude weitergereicht statt ins fokussierte Fenster getippt wird.
- Tippen erfolgt über `ydotool` (`dictate/typing_backend.py`), inklusive
  Tastatur-Layout-Fixup für DE/QWERTZ (`dictate/keymap.py`) und
  Sound-/Notify-Feedback (`dictate/feedback.py`) -- nur auf dem
  `toggle_record`-Pfad, nicht bei `start_recording`/`stop_recording`.
- Mic-Exklusivität: Diktieren und Sprach-Dialog können nicht gleichzeitig
  laufen, da beide dasselbe Mikrofon über denselben Daemon-Zustand
  beanspruchen -- ein zweiter `start_recording`/`toggle_record`-Aufruf
  während einer laufenden Aufnahme bekommt vom Daemon ein `busy` zurück.

### Setup

```bash
uv sync   # falls noch nicht geschehen
```

**ydotool** (Text-Injection unter Wayland) einrichten -- Skript vorher
anschauen, da es `sudo` braucht:

```bash
./scripts/setup_ydotool.sh
```

Danach einmal komplett aus- und wieder einloggen (siehe Skript-Ausgabe).

**systemd-User-Service** installieren:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/parakeet-dictate.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

Bewusst ohne `[Install]`-Sektion -- der Service startet nie automatisch,
nur über `enable`/`disable`.

**Hotkey binden** (GNOME Settings -> Keyboard -> View and Customize
Shortcuts -> Custom Shortcuts):
- Command: `/home/chris/research/speech-to-speech/.venv/bin/parakeet-dictate toggle`
- Shortcut: z.B. `Super+D`

### Nutzung

```bash
parakeet-dictate enable    # lädt Modell (ein paar Sekunden)
# ... in ein Textfeld klicken, Hotkey drücken, sprechen, nochmal drücken ...
parakeet-dictate status
parakeet-dictate disable   # gibt Speicher wieder frei
```

## TTS-Daemon (`qwen-tts`)

Analog zum Diktier-Daemon: ein Hintergrunddienst (`src/speech_to_speech/tts_daemon/`)
hält das Qwen3-TTS-Modell warm und übernimmt auch die Wiedergabe selbst,
direkt über die lokalen Lautsprecher -- der Sprach-Dialog (`app.py`) ist
über `tts_client.py` nur noch Client, kein Modell-/Playback-Handling mehr im
App-Prozess (siehe `docs/architecture-proposal.md`, "Daemon-Aufspaltung",
Schritte 5+6). Im Unterschied zum Diktier-Daemon gibt es hier keinen
Hotkey-Nutzer -- nur `app.py` spricht ihn an, über `speak`/`stop`.

Ein `stop()` (Barge-in) kann eine bereits laufende Chunk-Generierung nicht
sofort unterbrechen, nur zwischen zwei Chunks -- in seltenen Fällen kann
eine Generierung hängen bleiben. Ein Watchdog in `daemon.py` beendet den
Daemon-Prozess dann selbst, `Restart=on-failure` in der systemd-Unit startet
ihn automatisch neu (~10s Modell-Ladezeit statt unbegrenztem Hängen). Siehe
`docs/architecture-proposal.md`, Nachtrag zu Schritt 5.

`playback.py` sucht das Ausgabegerät explizit per Namen (`JABRA_DEVICE_NAME`)
statt sich auf das System-Standard-Ausgabegerät zu verlassen -- auf macOS
folgt nur das Standard-*Eingabe*gerät automatisch dem verbundenen Jabra, die
Ausgabe bleibt sonst auf den Mac-Lautsprechern hängen, selbst während das
Headset als Mikrofon genutzt wird. Ist kein Jabra verbunden, fällt die
Wiedergabe auf das System-Standardgerät zurück (gleiches
Graceful-Degradation-Muster wie der Jabra-Knopf).

### Setup

```bash
mkdir -p ~/.config/systemd/user
cp systemd/qwen-tts.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

Bewusst ohne `[Install]`-Sektion -- der Service startet nie automatisch, nur
über `enable`/`disable`.

### Nutzung

```bash
qwen-tts enable     # lädt Modell (ein paar Sekunden)
qwen-tts status
qwen-tts disable    # gibt GPU-Speicher wieder frei
```

## Telegram-Bot (Handy-Zugang)

Vom Handy aus per Telegram eine Text- oder Sprachnachricht an den Agenten
schicken, Antwort als Text zurück -- kein TTS-Rückkanal, bewusst text-only
(siehe `docs/telegram-bot-proposal.md` für die Architektur-Entscheidungen).
Long-Polling, kein offener Port. Sprachnachrichten werden per `ffmpeg` von
Ogg/Opus nach PCM dekodiert und über den bestehenden STT-Daemon
transkribiert (`transcribe`-Kommando, `dictate/daemon.py`); das Transkript
kommt zuerst als eigene Nachricht zurück, danach die Antwort. Pro Chat-ID
läuft eine eigene `AgentConversation` (Default-Backend `claude`, siehe
`agent_backend.py`) für Kontext über mehrere Nachrichten -- nicht über
Neustarts des Bot-Prozesses hinweg persistiert.

### Setup

1. Neuen Bot bei [@BotFather](https://t.me/BotFather) anlegen (eigener Bot
   empfohlen statt Wiederverwendung des Erinnerungs-Bots, damit sich
   Erinnerungen und Assistenten-Antworten nicht vermischen), Token notieren.
2. Eigene Chat-ID herausfinden: dem neuen Bot einmal schreiben, dann
   `https://api.telegram.org/bot<TOKEN>/getUpdates` aufrufen.
3. `TELEGRAM_BOT_TOKEN` und `TELEGRAM_ALLOWED_CHAT_IDS` in `.env` eintragen
   (siehe `.env.example`) -- alle anderen Chat-IDs werden stillschweigend
   ignoriert.
4. `parakeet-dictate enable` muss laufen (Sprachnachrichten brauchen den
   STT-Daemon; reiner Text-Betrieb würde ohne ihn gehen, der Bot verlangt
   ihn aber beim Start, siehe `telegram_bot/daemon.py`).
5. systemd-User-Service installieren -- **im Unterschied** zu den beiden
   Daemons oben startet dieser Service bewusst automatisch beim Login
   (`[Install]`-Sektion), da der ganze Zweck ist, jederzeit vom Handy aus
   erreichbar zu sein:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/telegram-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now telegram-bot.service
```

### Nutzung

Läuft nach dem Setup dauerhaft im Hintergrund. Für manuelle Kontrolle:

```bash
speech-to-speech-telegram-bot status
speech-to-speech-telegram-bot stop
speech-to-speech-telegram-bot start
```

## Bekannte Grenzen (PoC-Stand)

- Sentence-Streaming reduziert die gefühlte Latenz (Time-to-first-audio),
  senkt aber nicht die rohe TTS-Generierungsgeschwindigkeit (~0.7x Echtzeit
  auf dieser GPU ohne flash-attn) -- bei einer einzelnen kurzen
  Antwort-"Satz" bringt es nichts.
- Kein Review/Edit-Schritt -- Transkript geht direkt an Gemini.
- `JABRA_TOGGLE_KEY` ist für dieses eine Gerät hartkodiert (wie die
  DE-Layout-Tabelle in `parakeet-dictate`).
- Läuft manuell im Vordergrund, kein systemd-Service.
