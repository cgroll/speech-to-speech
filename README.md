# speech-to-speech

Proof of concept: Sprache rein, Sprache raus. Ein Knopf am Jabra-Headset
(oder wahlweise eine lokale Tastenkombination) startet/stoppt die Aufnahme
(push-to-toggle, wie bei `parakeet-dictate`), das Gesagte wird lokal
transkribiert (Parakeet/CPU), an Gemini geschickt, und die Antwort wird
lokal per Qwen3-TTS (GPU, Stimme "aiden") vorgelesen.

Ein einziger Prozess hält beide Modelle warm und nimmt Toggle-Auslöser aus
zwei Quellen entgegen: `evdev` liest den Jabra-Knopf direkt, und ein
schlanker Unix-Socket (`toggle_socket.py`) nimmt Kommandos von einem
optionalen lokalen Hotkey entgegen (siehe Setup Schritt 5). Fehlt der Jabra
(nicht angeschlossen), läuft die App trotzdem weiter -- nur über den
Socket-Pfad.

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
uv run speech-to-speech
```

Wartet, bis beide Modelle geladen sind ("Ready."), dann: Knopf drücken
(Jabra oder lokaler Hotkey), sprechen, nochmal drücken -> Transkript +
Gemini-Antwort erscheinen im Log, die Antwort wird gesprochen. Beenden mit
Ctrl+C.

## Architektur

- `audio_io.py` -- Mikrofonaufnahme mit VAD-Segmentierung (adaptiert aus
  `parakeet-dictate/src/parakeet_dictate/audio.py`) + Wiedergabe.
- `stt.py` -- Parakeet (ONNX, int8, CPU).
- `tts.py` -- Qwen3-TTS CustomVoice (GPU, fester Sprecher "aiden").
- `llm.py` -- Gemini-Chat-Session mit Konversationshistorie über die
  Laufzeit der App. Bewusst eine einzelne kleine `send()`-Funktion -- das
  ist der Punkt, an dem später ein anderes Backend (z.B. Claude Code)
  eingehängt werden kann.
- `input_button.py` -- `evdev`-Listener für den Jabra-Knopf.
- `toggle_socket.py` -- Unix-Socket-Server/-Client für den optionalen
  lokalen Hotkey (GNOME-Shortcut -> `speech-to-speech-toggle` -> Socket).
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

## Bekannte Grenzen (PoC-Stand)

- Sentence-Streaming reduziert die gefühlte Latenz (Time-to-first-audio),
  senkt aber nicht die rohe TTS-Generierungsgeschwindigkeit (~0.7x Echtzeit
  auf dieser GPU ohne flash-attn) -- bei einer einzelnen kurzen
  Antwort-"Satz" bringt es nichts.
- Kein Review/Edit-Schritt -- Transkript geht direkt an Gemini.
- `JABRA_TOGGLE_KEY` ist für dieses eine Gerät hartkodiert (wie die
  DE-Layout-Tabelle in `parakeet-dictate`).
- Läuft manuell im Vordergrund, kein systemd-Service.
