# Backlog

Kleinere offene Punkte, die noch nicht angegangen wurden. Für größere
Architektur-Vorschläge siehe stattdessen die eigenen Dateien unter `docs/`
(z.B. `architecture-proposal.md`).

## Web-Cockpit: Scrollen im Chat-Verlauf funktioniert nicht

Status: Notiert am 2026-08-12, noch nicht untersucht.

Im Gradio-Cockpit (`cockpit.py`) lässt sich im Chat-Messages-Bereich nicht
nach oben scrollen, um ältere Nachrichten der laufenden Session einzusehen.

### Nächste Schritte
- Reproduzieren und eingrenzen, ob es an der Gradio-Chatbot-Komponente selbst
  liegt (Konfiguration, CSS/Höhe) oder am periodischen Live-Refresh
  (`COCKPIT_POLL_SECONDS`), der die Scroll-Position bei jedem Poll
  zurücksetzen könnte.

## Frühere Sessions wieder aufnehmen können

Status: Cockpit-Teil am 2026-08-12 umgesetzt (`sessions.py`,
`App.resume_session()`, Auswahl-Liste im Cockpit). Offen: Start-Flag-Variante
ohne UI (siehe unten).

Es stellte sich heraus, dass eine eigene JSON-Persistierung gar nicht nötig
ist: Der Claude Agent SDK schreibt jede Session ohnehin als JSONL unter
`~/.claude/projects/<projekt>/` und bringt `list_sessions()` /
`get_session_messages()` sowie `ClaudeAgentOptions(resume=session_id)` schon
mit. Umgesetzt wurde daher:
- `sessions.py`: dünner Wrapper um `list_sessions()`/`get_session_messages()`
  -- liefert Anzeige-Labels (Titel + Zeitstempel) sowie eine Rekonstruktion
  des Cockpit-Chatverlaufs (nur Text-Turns, ohne Tool-Calls/Thinking-Blöcke)
  aus einer alten Session.
- `llm.py`: `ClaudeCodeConversation` akzeptiert jetzt `resume: str | None`.
- `app.py`: `App.reset()` und die neue `App.resume_session(session_id)`
  teilen sich jetzt `_abort_current_turn()` (Abbruch/Teardown); Resume baut
  zusätzlich `self._history` aus der alten Session neu auf.
- `cockpit.py`: Accordion "Frühere Sessions" mit Radio-Liste (Label +
  Session-ID), "Liste aktualisieren" und "Ausgewählte Session fortsetzen".
  Liste wird nicht vom schnellen Poll-Timer aktualisiert (Dateisystem-Scan),
  sondern nur bei Seitenaufruf sowie nach Reset/Resume.

### Nächste Schritte
- Manuell im Cockpit durchklicken/verifizieren (Listenanzeige, Resume,
  Chatverlauf-Wiederherstellung).
- Später ggf. Start-Flag (`--resume [SESSION_ID]` / `--continue`), damit eine
  Session auch ganz ohne Cockpit-UI gesetzt werden kann.
- Liste enthält aktuell alle Claude-Code-Sessions dieses Projektverzeichnisses
  (auch normale CLI-Coding-Sessions, nicht nur Sprach-Turns) -- eventuell
  später filtern, falls das in der Praxis stört.

## Sprachausgabe liest Zwischenschritte/Nachdenktext statt nur die finale Antwort

Status: Notiert am 2026-08-12, noch nicht untersucht.

Beim Sprechen kommt aktuell alles "in einem Rutsch" als Sprachausgabe: nicht
nur die finale Antwort, sondern auch Zwischenkommentare/"Nachdenkgedanken"
und Kommentare zu einzelnen Zwischenschritten (z.B. Tool-Aufrufen).

Ursache vermutlich in `llm.py`, `ClaudeCodeConversation._query()`: dort
werden aktuell *alle* `TextBlock`-Textstücke aus *jeder* `AssistantMessage`
der Runde eingesammelt (`parts.append(block.text)`) und am Ende zu einem
einzigen `reply`-String zusammengefügt, der dann komplett an `App._speak()`
/ die TTS-Ausgabe geht -- unabhängig davon, ob der Text ein Zwischenkommentar
vor einem Tool-Aufruf war oder die eigentliche finale Antwort.

### Ideen / offene Fragen
- Option A: Zwischenkommentare/Nachdenktext laufend/inkrementell als
  Sprachausgabe streamen, sobald sie eintreffen (statt am Stück nach
  Fertigstellung).
- Option B: Zwischenkommentare komplett aus der Sprachausgabe ausblenden --
  nur die finale Antwort wird gesprochen.
- Option C: Zwischenschritte nur als Text im Cockpit-Chatverlauf anzeigen
  (nicht sprechen), und ausschließlich die finale Antwort per TTS ausgeben.
- Dafür müsste `_query()` vermutlich unterscheiden können, welche
  `AssistantMessage`/`TextBlock`s "Zwischenkommentar vor einem Tool-Aufruf"
  sind vs. die abschließende Antwort ohne folgenden Tool-Use (z.B. anhand von
  `stop_reason`/ob danach noch Tool-Use-Blöcke folgen), und müsste das an
  `App._respond()`/`_speak()` getrennt weiterreichen statt nur einen
  zusammengefassten `reply`-String zurückzugeben.

## Mehrere Agent-Backends (Claude Code / Pi)

Status: Punkte 1 und 3 am 2026-08-12 umgesetzt (`agent_backend.py`,
`pi_agent.py`, `sessions.py`, `App`/Cockpit-Anpassungen, `--agent`-Flag in
`cli.py`). Punkt 2 (Workspace-Eingrenzung) bewusst zurückgestellt -- eigenes,
nicht triviales Thema, wird separat angegangen. Kontext: Idee, den Service
wahlweise mit dem Pi Coding Agent (`@earendil-works/pi-coding-agent`, CLI
`pi`, siehe `~/research/pi-test`) statt/neben dem Claude Agent SDK starten zu
können.

### 1. Session-History-Format ist Claude-SDK-spezifisch (umgesetzt)

Der oben beschriebene "Frühere Sessions"-Mechanismus (`sessions.py`) baut
direkt auf Claude-Agent-SDK-Eigenheiten auf: JSONL unter
`~/.claude/projects/<projekt>/`, `list_sessions()`/`get_session_messages()`,
`ClaudeAgentOptions(resume=session_id)`. Pi hat ein eigenes, anders
strukturiertes Session-Format (`~/.pi/agent/sessions/<projekt>/*.jsonl`,
gesteuert über `--session`/`--session-id`/`--continue`/`--resume`) --
vermutlich nicht 1:1 kompatibel zu unserem Parsing.

Entscheidung (2026-08-12): keine getrennten Listen pro Backend. Stattdessen
eine einzige, gemeinsame "Frühere Sessions"-Liste im Cockpit, in der pro
Eintrag vermerkt ist, mit welchem Agenten-Backend die Session geführt wurde.
Fortsetzen ("Ausgewählte Session fortsetzen") knüpft dann automatisch wieder
an genau dieses Backend an -- kein Backend-Wechsel innerhalb einer
fortgesetzten Session.

Praktisch heißt das: `sessions.py` bekommt pro Backend weiterhin eigene
`list_sessions()`/`get_session_messages()`-Implementierungen (die
Formate/Speicherorte bleiben ja unterschiedlich -- Claude JSONL unter
`~/.claude/projects/...` vs. Pis Format unter `~/.pi/agent/sessions/...`),
aber eine gemeinsame Merge-Schicht führt beide Listen zu einer sortierten
Gesamtliste zusammen, tag't jeden Eintrag mit dem Agenten-Namen (Label z.B.
"Claude Code" / "Pi") und zeigt das im Cockpit mit an. `App.resume_session()`
liest dieses Tag und instanziiert dafür die passende Backend-Klasse
(`ClaudeCodeConversation` vs. künftiges `PiAgentConversation`) statt wie
aktuell immer `ClaudeCodeConversation`. Die App-eigene `self._history` (nur
Text-Turns, Backend-unabhängig) bleibt weiterhin die Quelle für die
Cockpit-Anzeige der laufenden Session.

### 2. Wie sagt man einem Agenten sauber, in welchem Workspace er bleiben soll? (umgesetzt am 2026-08-14)

Ziel: Agent arbeitet standardmäßig nur innerhalb eines festgelegten
Workspace-Verzeichnisses (Dateien lesen/schreiben, Tool-Aufrufe) und
verlässt es nur auf explizite Aufforderung.

Entscheidung: gleiches "einmal pro neuer Session, kein Wechsel mittendrin"
-Muster wie die Agenten-Wahl (Punkt 3 unten) -- kein separater
Sprachbefehl/Slash-Command, der mitten in einer laufenden Session den
Workspace verschiebt.

Umgesetzt:
- **Claude Agent SDK** (`llm.py`): `ClaudeAgentOptions.cwd` wird jetzt
  gesetzt (`add_dirs`/`can_use_tool`-Hook bewusst nicht -- `cwd` allein
  reicht für "einen Workspace pro Session", die härtere
  Pfad-für-Pfad-Durchsetzung über `can_use_tool` ist nicht Teil dieser
  Umsetzung).
- **Pi** (`pi_agent.py`): Subprozess-`cwd` ist jetzt der Session-Workspace
  statt fest `PROJECT_DIR`. Pis eigene Guardrails-Extension bleibt wie
  beschrieben global auf `~/.agents`/`~/research` -- ein Workspace außerhalb
  davon wird von Pi also nicht zusätzlich blockiert, aber auch nicht
  spezifisch freigegeben; das enger zu fassen ist weiterhin offen.
- **Backend-unabhängig** (`agent_backend.py`): `workspace_instruction()`
  hängt die weiche System-Prompt-Instruktion an, kombiniert mit der harten
  `cwd`-Durchsetzung oben. `resolve_workspace()` validiert/normalisiert
  einen nutzereingegebenen Pfad (`~`-Expansion, Existenz-/Verzeichnis-Check)
  -- von beiden UIs unten verwendet, bevor der Pfad an `App.reset()`/
  `_reset_session()` durchgereicht wird.
- Workspace-Pfad pro Session kommt jetzt von: `config.DEFAULT_WORKSPACE`
  (=`PROJECT_DIR`) als Startwert, dann `App.reset(workspace=...)`
  (Cockpit) bzw. `/workspace <Pfad>` (Telegram) für jede neue Session.
  `App`/`_ChatSession` merken sich den aktuellen Workspace wie den
  aktuellen Agenten und zeigen ihn: `get_stats_text()`s "Workspace: ..."
  im Cockpit, `/workspace` sowie die Bestätigung nach `/new`/`/workspace`
  in Telegram.
- Cockpit (`cockpit.py`): "Workspace für neue Session"-Accordion mit einer
  Textbox (Pfad direkt eingeb-/editierbar) und einem `gr.FileExplorer` als
  reinem Ordner-Browser (`glob=""` lässt keine Datei durch, Ordner werden
  davon unabhängig immer gelistet) im Home-Verzeichnis; Auswahl dort füllt
  die Textbox. "Neue Session" validiert den Textbox-Wert über
  `resolve_workspace()`, fällt bei ungültigem Pfad auf den bisherigen
  Workspace zurück (mit Warnung) statt die Session kaputt zu starten.
- Resume einer alten Session (`App.resume_session()`) übernimmt bewusst den
  *aktuellen* Workspace statt zu versuchen, den ursprünglichen
  wiederherzustellen -- der wird bislang nirgends pro Session
  mitgeschrieben.

### Nächste Schritte
- Manuell verifizieren: Cockpit-Ordner-Browser durchklicken, Pfad
  übernehmen, "Neue Session" -- prüfen, dass der Agent tatsächlich im neuen
  Verzeichnis arbeitet (nicht nur `cwd` gesetzt, sondern auch tatsächlich
  genutzt). Telegram `/workspace <Pfad>` ebenso.
- Offen geblieben: `can_use_tool`-Hook (Claude) / engere `pathAccess`-Liste
  (Pi) für harte Pfad-für-Pfad-Durchsetzung statt nur `cwd` als Startpunkt
  -- aktuell kann der Agent trotz gesetztem Workspace mit `bypassPermissions`
  weiterhin überall lesen/schreiben, `cwd` ist nur der Default, keine
  Sandbox.

### 3. Wie wählt man den Agenten aus? (umgesetzt)

Entscheidung (2026-08-12): zwei Auswahlpunkte, kein Wechsel mitten in einer
laufenden Session.
- Start-Flag (`speech-to-speech --agent claude|pi`, Default `claude`, siehe
  `cli.py`) legt fest, mit welchem Backend die allererste Session eines
  Prozessstarts läuft.
- Wichtiger/bevorzugt: beim Start einer *neuen* Session (Cockpit-Button
  "Neue Session", also `App.reset()`) lässt sich der Agent explizit
  mitbenennen -- nicht nur einmal beim App-Start festgelegt. Passt zur
  Session-Tagging-Entscheidung aus Punkt 1: jede Session trägt ihr
  Agenten-Label ab dem Moment, in dem sie beginnt.

Skizze für die Umsetzung:
- Gemeinsames Interface (Protocol/ABC) mit `send()`, `cancel()`, `close()`,
  das `ClaudeCodeConversation` (`llm.py`) bereits erfüllt; neue
  `PiAgentConversation` (neues Modul, z.B. `pi_agent.py`) implementiert es
  gegen die `pi`-CLI als Subprozess (`pi --print --mode json --session-id
  <id> --system-prompt ... "<text>"`), `cancel()` beendet den laufenden
  Subprozess.
- Kleine Factory (z.B. `make_llm(agent: str) -> AgentConversation`) an einer
  Stelle statt der bisher drei verstreuten `ClaudeCodeConversation()`-Aufrufe
  (`App.load()`, `App.reset()`, künftig `App.resume_session()`).
- `App` merkt sich zusätzlich zu `self._llm` den aktuellen Agenten-Namen
  (`self._agent_name`), für Anzeige im Cockpit und zum Tagging beim
  Schreiben/Anzeigen der Session-History (siehe Punkt 1).
- `App.reset()` bekommt einen optionalen `agent`-Parameter; Cockpit-UI dafür
  vermutlich eine kleine Auswahl (Radio/Dropdown "Claude" / "Pi") direkt
  neben dem "Neue Session"-Button, Default = zuletzt genutzter bzw.
  Start-Flag-Wert.
- Hängt inhaltlich an Punkt 2 (Workspace): die Factory müsste den
  Workspace-Pfad an beide Backends gleichermaßen durchreichen (`cwd` beim
  Claude SDK, Subprozess-`cwd` bei Pi).

## Daemon-Neustart aus der App/dem Cockpit heraus

Status: Umgesetzt am 2026-08-14 (`daemon_control.py`,
`App.restart_stt_daemon()`/`restart_tts_daemon()`, Cockpit-Buttons,
Telegram-Slash-Commands `/restart_stt`/`/restart_tts`).

Bislang ließ sich weder der STT- noch der TTS-Daemon von innerhalb der App
(Cockpit) neu starten -- nur von außen per Terminal (`qwen-tts
disable`/`enable`, `parakeet-dictate disable`/`enable`, die im Hintergrund
`systemctl --user stop`/`start` auf die jeweilige Unit ausführen). Für den
TTS-Daemon gibt es zwar schon einen automatischen Notfall-Mechanismus (Watchdog
+ `Restart=on-failure` in `qwen-tts.service`, siehe `tts_daemon/daemon.py`),
aber keinen manuell auslösbaren Neustart für den Fall, dass ein Daemon zwar
noch antwortet, aber sich falsch/festgefahren verhält, ohne dass der Watchdog
anschlägt.

Umgesetzt: `daemon_control.py` kapselt den `systemctl --user restart`-Aufruf
für beide Units (Restart statt separatem stop+start, da systemd das schon
atomar macht) mit den `SERVICE_NAME`-Konstanten aus `dictate/cli.py` und
`tts_daemon/cli.py`. `App.restart_stt_daemon()`/`restart_tts_daemon()`
(`app.py`) brechen eine laufende Aufnahme bzw. Wiedergabe vorher sauber ab
(gleiche `stt_client.stop_recording()`/`tts_client.stop()`-Aufrufe wie beim
Barge-in), bevor der jeweilige Daemon neu gestartet wird -- "thinking" bleibt
unangetastet, da dabei kein Daemon involviert ist. Das Cockpit (`cockpit.py`)
hat dafür zwei neue Buttons neben "Neue Session"; der Telegram-Bot
(`telegram_bot/daemon.py`) die Slash-Commands `/restart_stt`/`/restart_tts`
(Chat-unabhängig, da der Daemon geteilt wird).

### Nächste Schritte
- Manuell verifizieren: Cockpit-Buttons und Telegram-Commands während
  laufender Aufnahme/Wiedergabe auslösen und prüfen, dass sauber in "idle"
  zurückgekehrt wird statt in einem hängenden Zustand.
- `systemctl --user` aus dem App-/Bot-Prozess heraus war unproblematisch
  (gleicher User, gleiche Session) -- keine offene Berechtigungsfrage mehr.

## Bilder an den Nutzer senden

Status: Umgesetzt am 2026-08-14 (`image_tool.py`, `App._append_image()`,
Telegram `_make_on_image()`/`_send_image()`), Claude-Backend only.

Der Agent hat vollen Dateisystemzugriff (Bash, Write) und kann Bilder
erzeugen oder finden, hatte aber keine Möglichkeit, sie dem Nutzer auch
tatsächlich zu zeigen -- ein Dateipfad in der (per TTS vorgelesenen)
Text-Antwort ist für den Nutzer unsichtbar/nutzlos.

Entscheidung: ein Tool (`show_image(path, caption)`), keine Skill -- die
Tool-`description` selbst lehrt das Modell, wann/wie es aufzurufen ist
(siehe image_tool.py), eine zusätzliche Skill-Ebene wäre für eine einzelne,
schmal geschnittene Funktion unnötige Indirektion.

Umgesetzt:
- `image_tool.py`: `make_show_image_tool(workspace, on_image)` baut ein
  In-Process-SDK-Tool (`@tool` + `create_sdk_mcp_server`, Claude Agent SDK)
  pro Session -- Closure über Workspace (zum Auflösen relativer Pfade) und
  eine Delivery-Callback `on_image(path, caption)`, die der jeweilige Aufrufer
  (App fürs Cockpit, Telegram-Bot pro Chat) mitgibt. Validiert Pfad
  (existiert, ist Datei, MIME-Typ beginnt mit `image/`) bevor `on_image`
  aufgerufen wird; Fehler gehen als `is_error`-Tool-Result ans Modell zurück,
  nicht als Exception.
- Claude-only: `llm.py`s `ClaudeCodeConversation` registriert das Tool nur,
  wenn ihr ein `on_image`-Callback mitgegeben wurde, und hängt dann
  zusätzlich `SHOW_IMAGE_INSTRUCTION` an den System-Prompt. Pi bekommt das
  Tool (noch) nicht -- `pi_agent.py` unverändert, `create_conversation()`
  reicht `on_image` nur an den Claude-Zweig durch (siehe docs, warum Pi
  schwerer ist: Subprozess pro Turn, kein In-Process-Callback-Pfad).
- Cockpit (`app.py`): `App._append_image()` hängt eine History-Message mit
  `content=[{"path": ...}, caption]` an -- `gr.Chatbot` rendert das
  automatisch als Bild inline (verifiziert: Gradios `MessageContent`
  akzeptiert Datei-Dicts direkt, kein Cockpit-Code-Änderung nötig).
- Telegram (`telegram_bot/daemon.py`): `_make_on_image(chat_id, loop)`
  baut pro Chat eine Callback, die den Tool-Aufruf (läuft auf dem SDKs
  eigenem Event-Loop-Thread) über `run_coroutine_threadsafe()` zurück auf
  den Bot-eigenen Event-Loop hebt (`loop` wird dafür an den bestehenden
  Aufrufstellen, die schon `async def` sind, per `asyncio.get_running_loop()`
  eingefangen) und dort `bot.send_photo()` aufruft -- unabhängig vom
  ursprünglich auslösenden `Update`, da der Tool-Aufruf ja asynchron zur
  laufenden `send()` passiert.
- Manuell end-to-end verifiziert (`ClaudeCodeConversation` direkt, ohne
  App/Bot): Agent erzeugt PNG per Bash+PIL (absoluter und relativer Pfad),
  ruft `show_image` auf, Callback feuert mit aufgelöstem Pfad + Caption;
  Nicht-Bild-Datei wird korrekt mit `is_error` abgelehnt, Callback feuert
  nicht.

### Nächste Schritte
- Manuell im echten Cockpit/Telegram durchklicken (nicht nur die
  `ClaudeCodeConversation`-Ebene direkt) -- insbesondere ob `gr.Chatbot` das
  Bild wirklich inline rendert und `bot.send_photo()` in einem echten Chat
  ankommt.
- Pi-Unterstützung offen (bräuchte einen echten MCP-Server-Prozess statt
  einer In-Process-Closure, siehe image_tool.py's Docstring).
- Aktuell nur Bilder (MIME-Präfix `image/`); andere Dateitypen (PDF, Audio,
  beliebige Dokumente) wären ein separates, ähnlich gebautes Tool
  (`send_document` o.ä.), nicht Teil dieser Umsetzung.
