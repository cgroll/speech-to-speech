# Architektur-Vorschlag: Telegram-Bot als Handy-Zugang

Status: Umgesetzt am 2026-08-12 (siehe README.md, Abschnitt
"Telegram-Bot (Handy-Zugang)", für Setup/Nutzung). Ausgangspunkt war die
Frage, wie sich der Sprachassistent vom Handy aus per Sprach- oder
Textnachricht erreichen lässt, ohne eine eigene Mobile-App bauen zu müssen.
Offen bleibt nur noch der manuelle Teil aus "Nächste Schritte" (Bot bei
BotFather anlegen, `.env` befüllen, systemd-Unit installieren, End-to-End
verifizieren) -- Code und Doku sind fertig.

## Ziel

Vom Handy aus per Telegram entweder eine Sprach- oder eine Textnachricht an
den Assistenten schicken und eine Textantwort zurückbekommen. Keine
Sprachausgabe auf diesem Weg (kein TTS-Rückkanal) -- bewusst text-only, um
den Umfang klein zu halten. Zwei Eingabewege, die am Ende in derselben
Agent-Anbindung zusammenlaufen:

- **Textnachricht** -> direkt an den Agenten, Antwort als Text zurück.
- **Sprachnachricht** -> erst durch Spracherkennung, das Transkript wird zur
  Kontrolle als eigene Nachricht zurückgeschickt (damit sich Erkennungs- von
  Verständnisfehlern unterscheiden lassen), danach genau wie eine
  Textnachricht an den Agenten weitergereicht, Antwort als Text zurück.

Kein Audio-Echo der eingehenden Sprachnachricht (verworfen -- die eigene
gesendete Nachricht bleibt ohnehin im Chatverlauf sichtbar, das nochmal vom
Bot zurückzuschicken bringt nichts zusätzlich).

## Ist-Zustand

- Telegram wird in diesem Account bereits zweimal genutzt, aber nur als
  Einbahnstraße nach draußen, nie zum Empfangen:
  - `~/research/gcp-telegram-reminders`: GCP Cloud Function, verschickt
    zeitgesteuerte Erinnerungen über den Bot (Cloud Tasks -> Cloud Function
    -> `sendMessage`).
  - `~/.agents/scripts/send_telegram.sh`: einfaches Skript, schickt per curl
    eine Nachricht über denselben Bot-Token/Chat-ID.
  - Beide zusammen bestätigen: ein Bot mit Token und die eigene Chat-ID
    existieren schon, es gibt aber keinen Listener, der eingehende
    Nachrichten (Text oder Sprache) entgegennimmt -- weder Polling noch
    Webhook.
- Der STT-Daemon (`speech_to_speech/dictate/daemon.py`) ist komplett auf
  Live-Mikrofon-Aufnahme ausgelegt: er besitzt Mikrofon und VAD-Segmentierung
  selbst (`dictate/audio.py`, `Recorder`), die bestehenden Befehle
  (`toggle_record`, `start_recording`/`stop_recording`) starten/stoppen
  jeweils eine eigene Aufnahme. Es gibt aktuell keinen Befehl, der eine
  bereits vorliegende Audiodatei/-bytes einfach nur transkribiert.
- Telegram-Sprachnachrichten liegen als Ogg/Opus vor, nach Dekodierung 16 kHz
  Mono -- deckt sich zufällig exakt mit `dictate/audio.py`s
  `SAMPLE_RATE = 16_000`, auf das Parakeet eingestellt ist. Für die
  Dekodierung von Opus nach PCM wird trotzdem ein externer Schritt (ffmpeg)
  gebraucht, Telegram liefert kein rohes PCM.
- Agent-Anbindung (`agent_backend.create_conversation`, `llm.py`/
  `pi_agent.py`) ist bereits Backend-unabhängig und wird unverändert
  wiederverwendet -- der Bot braucht nur eine eigene, pro Chat gehaltene
  `AgentConversation`-Instanz für Kontext über mehrere Nachrichten hinweg,
  analog zu `App`s `self._llm`.

## Vorschlag

**1. STT-Daemon: neuer Befehl `transcribe`**

Ergänzung in `dictate/daemon.py`s `handle_command` (analog zum bestehenden
`status`-Befehl, kein neuer Zustand/Thread nötig, da synchron und kurz):
nimmt fertige Audiodaten (16 kHz, mono, float32 -- oder base64-kodiertes WAV,
Details bei Umsetzung) entgegen und ruft direkt `self._model.recognize(...)`
auf, ohne `Recorder`/VAD zu benutzen. Bypass der Live-Aufnahme-Mechanik
komplett. `dictate/protocol.py` bleibt unverändert (gleiches
Socket-JSON-Framing), nur ein neues Kommando.

Offener Punkt: der Daemon bearbeitet aktuell eine Verbindung nach der
anderen (`server.listen(1)`, ein `accept()`-Loop). Trifft ein
`transcribe`-Aufruf vom Bot ein, während der Desktop-Nutzer gerade mitten in
einer Live-Aufnahme ist (`state == "recording"`), muss `transcribe`
vermutlich denselben "busy"-Fehler zurückgeben wie andere Befehle in dem
Zustand -- zu klären, ob das in der Praxis stört (eine Person, kann ohnehin
nicht gleichzeitig ins Mikrofon sprechen und eine Sprachnachricht am Handy
aufnehmen, vermutlich unproblematisch).

**2. Neues Modul: Telegram-Bot-Daemon**

Neues Paket, z.B. `speech_to_speech/telegram_bot/`, nach demselben
Grundmuster wie `dictate/` und `tts_daemon/` (eigenständiger Prozess, eigene
systemd-Unit). Neue Abhängigkeit: `python-telegram-bot`. Long-Polling statt
Webhook -- keine offene Portfreigabe, kein öffentlicher Server nötig, der
Bot macht nur ausgehende Verbindungen zu Telegrams Servern.

Ablauf pro eingehender Nachricht:
- Absender-Chat-ID gegen eine Allowlist (nur die eigene Chat-ID) prüfen,
  alles andere ignorieren.
- Text -> direkt an Schritt "Agent" (siehe unten).
- Sprachnachricht -> Datei via Telegram-API herunterladen, mit ffmpeg von
  Ogg/Opus nach PCM dekodieren, per neuem `transcribe`-Kommando an den
  STT-Daemon schicken, Transkript als eigene Telegram-Nachricht
  zurückschicken (Sichtbarkeit der Erkennungsqualität), Transkript-Text an
  Schritt "Agent" weiterreichen.
- Agent: pro Chat-ID eine im Speicher gehaltene `AgentConversation`-Instanz
  (`agent_backend.create_conversation`, Default-Backend wie bisher `claude`)
  für Kontext über mehrere Nachrichten; Text an `send()` übergeben, Antwort
  als Telegram-Nachricht zurückschicken.

**3. Konfiguration**

`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` (oder Allowlist mit mehreren IDs) in
der Projekt-`.env`, `config.py` lädt die ohnehin schon (`python-dotenv`,
siehe README Schritt 4 zu Gemini). Empfehlung: eigener, neuer Bot statt
Wiederverwendung des Reminder-Bots, damit sich Erinnerungen und
Assistenten-Antworten nicht im selben Chat vermischen -- kostet nur einen
BotFather-Aufruf, keine technische Notwendigkeit, aber sauberer.

**4. Deployment**

Neue systemd-User-Unit nach dem Muster von `parakeet-dictate.service`, aber
mit einem bewussten Unterschied: die bestehenden Daemons starten absichtlich
*nicht* automatisch (`enable`/`disable` von Hand). Für den Telegram-Bot
ergibt dagegen Auto-Start beim Login plus `Restart=on-failure` mehr Sinn,
weil der ganze Zweck ist, jederzeit vom Handy aus erreichbar zu sein, auch
wenn niemand gerade am Rechner sitzt (Restart-Vorbild: der bereits
vorhandene Watchdog-Restart beim TTS-Daemon, `systemd/qwen-tts.service`).
Optionaler `project.scripts`-Eintrag für eine dünne CLI (`enable`/`disable`/
`status`), falls manuelle Steuerung trotzdem gewünscht ist.

## Offene Fragen (Entscheidungen bei der Umsetzung)

- Eigener neuer Bot vs. Wiederverwendung des bestehenden Reminder-Bot-Tokens
  -- Empfehlung oben übernommen: eigener Bot. Muss manuell bei BotFather
  angelegt werden (Nächste Schritte, Punkt 1).
- Exaktes Datenformat für den neuen `transcribe`-Befehl: rohe
  Float32-Bytes, base64-kodiert (`dictate/protocol.py`, `encode_audio`/
  `decode_audio`) -- kein WAV-Container nötig, Sample-Rate/Kanalzahl sind
  durch `dictate/audio.py`s `SAMPLE_RATE` (16 kHz mono) ohnehin fix
  vorgegeben.
- `SYSTEM_PROMPT` unverändert übernommen wie vermutet -- unkritisch für den
  Telegram-Weg.
- STT-Daemon-Busy-Fall: `transcribe` guardet wie `start_recording` gegen
  `state != "idle"` und gibt denselben `busy: <state>`-Fehler zurück; der
  Bot zeigt den dann als Telegram-Fehlermeldung an. Nicht weiter
  verifiziert (eine Person, kann ohnehin nicht gleichzeitig ins Mikrofon
  sprechen und eine Sprachnachricht aufnehmen).
- Rate-Limiting/Fehlerverhalten: einfache Fehlermeldung als Textantwort,
  wie vermutet -- kein Retry/Timeout-Handling über die (großzügigen)
  Timeouts von `stt_client.transcribe()`/`AgentConversation.send()` hinaus.

## Nächste Schritte

1. Neuen Telegram-Bot bei BotFather anlegen, Token + eigene Chat-ID in
   `.env` eintragen (siehe README.md, Abschnitt "Telegram-Bot", Setup
   Schritte 1-3) -- einziger noch offener, manueller Schritt.
2. ~~`transcribe`-Befehl im STT-Daemon (`dictate/daemon.py`) ergänzen.~~
   Umgesetzt.
3. ~~`telegram_bot/`-Modul bauen: Long-Polling-Loop, Allowlist-Check,
   Text-/Sprach-Verzweigung, Agent-Anbindung pro Chat-ID.~~ Umgesetzt.
4. ~~systemd-Unit + `project.scripts`-Eintrag.~~ Umgesetzt
   (`systemd/telegram-bot.service`, `speech-to-speech-telegram-bot`/
   `-daemon` Scripts).
5. Manuell verifizieren: Textnachricht -> Antwort; Sprachnachricht ->
   Transkript-Nachricht -> Antwort; Zugriff von einer nicht-erlaubten
   Chat-ID wird ignoriert. Noch zu tun, sobald Schritt 1 erledigt ist.
