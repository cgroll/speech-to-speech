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

### 2. Wie sagt man einem Agenten sauber, in welchem Workspace er bleiben soll? (zurückgestellt, separates Thema)

Ziel: Agent arbeitet standardmäßig nur innerhalb eines festgelegten
Workspace-Verzeichnisses (Dateien lesen/schreiben, Tool-Aufrufe) und
verlässt es nur auf explizite Aufforderung.

Beide Backends bringen dafür Bausteine mit, die sich kombinieren lassen:
- **Claude Agent SDK**: `ClaudeAgentOptions.cwd` (Arbeitsverzeichnis
  setzen), `add_dirs` (zusätzlich erlaubte Verzeichnisse), sowie ein
  `can_use_tool`-Hook, über den sich Datei-Pfade pro Tool-Aufruf gegen den
  Workspace prüfen und bei Bedarf ablehnen/nachfragen lassen. Aktuell nutzt
  `llm.py` nichts davon -- `permission_mode="bypassPermissions"` ohne
  gesetztes `cwd`, d.h. de facto kein Workspace-Konzept.
- **Pi**: eigene Guardrails-Extension (`@aliou/pi-guardrails`, aktiv unter
  `~/.pi/agent/extensions/guardrails.json`) mit `pathAccess`-Feature:
  `mode` (u.a. `ask`) plus `allowedPaths`-Liste (Datei-/Verzeichnis-Einträge).
  Aktuell global auf `~/.agents` und `~/research` erlaubt -- das müsste für
  den Sprachassistenten-Anwendungsfall enger auf den jeweiligen Workspace
  gefasst werden.
- Zusätzlich, Backend-unabhängig: Instruktion im System-Prompt ("dein
  Workspace ist <Pfad>, verlasse ihn nur auf explizite Aufforderung") als
  weiche Steuerung, kombiniert mit der harten Durchsetzung über die
  jeweiligen Mechanismen oben.
- Offene Frage: woher kommt der Workspace-Pfad pro Session -- fester Wert in
  `config.py`, Start-Flag, oder (analog zum `switch`-Skill unter
  `~/.agents/skills/switch/SKILL.md`) dynamisch per Sprachbefehl wechselbar?

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
