# Spezifikation: Denkprozess-Kanal und Stop-Markierung

Status: Entwurf (2026-10-02)

Konkretisiert zwei Lücken, die beim Abgleich von
`docs/specs/core-dialog-loop.md` gegen den aktuellen Code aufgefallen sind:
den fehlenden Denkprozess-Kanal und die fehlende Markierung im Chatverlauf,
wenn ein Stop ohne Antwort endet. Baut direkt auf den dortigen Entscheidungen
auf (Abschnitt 3 "Ausgabe-Kanäle", Abschnitt 4 "Stop").

## 1. Ist-Zustand (Befund 2026-10-02)

- `llm.py`, `ClaudeCodeConversation._query()`: sammelt aktuell **alle**
  `TextBlock`-Textstücke aus **jeder** `AssistantMessage` der Runde ein --
  nicht nur aus der letzten -- und reicht sie als einen zusammengefügten
  `reply`-String weiter, der komplett an `_speak()` geht. Das heißt,
  Zwischenkommentare vor Tool-Aufrufen landen heute mit in der gesprochenen
  Antwort (siehe auch `docs/backlog.md`, "Sprachausgabe liest
  Zwischenschritte...", 2026-08-12 notiert, bisher nicht umgesetzt).
  `ThinkingBlock`-Inhalte werden dagegen komplett verworfen, gar nicht erst
  eingesammelt.
- `pi_agent.py`, `PiAgentConversation.send()`: nutzt nur die `text`-Blöcke
  der *letzten* Assistant-Message (`agent_end`-Event, `messages[-1]`) als
  `reply`. `thinking`/`toolCall`/`toolResult`-Blöcke werden ignoriert. Anders
  als beim Claude-Pfad ist hier das Problem "Zwischenkommentar vor
  Tool-Aufruf" vermutlich nicht vorhanden, weil ohnehin nur die letzte
  Message zählt -- zu verifizieren, sobald der Denkprozess-Kanal gebaut wird.
- `AgentConversation` (Protocol, `agent_backend.py`): `send()` liefert nur
  einen fertigen String zurück, kein zweiter Kanal.
- `App._respond()`/`_abort_current_turn()`: bei Stop/Barge-in während
  "thinking" wird die verworfene (Nicht-)Antwort nirgends vermerkt -- der
  Chatverlauf bleibt einfach bei der letzten Nutzer-Nachricht stehen, ohne
  jeden Hinweis, dass abgebrochen wurde.

## 2. Ziel

1. Ein zweiter, optionaler Ausgabe-Kanal ("Denkprozess") pro Turn, getrennt
   vom finalen Antwort-Kanal -- unabhängig vom Backend, über dasselbe
   Interface.
2. Eine sichtbare Markierung im Chatverlauf, wenn ein Turn per Stop beendet
   wird, bevor der Antwort-Kanal Inhalt hatte (Regel siehe
   core-dialog-loop.md Abschnitt 4 -- hier nur die konkrete Umsetzung).

## 3. Denkprozess-Kanal: Schnittstellen-Entwurf

Gleiches Muster wie das bestehende `on_image` (siehe `image_tool.py` /
`create_conversation()`): ein optionaler Callback, der bei Erzeugung der
Konversation mitgegeben wird und während `send()` null- oder mehrfach
aufgerufen wird, bevor `send()` den finalen Antwort-String zurückgibt.

- `agent_backend.create_conversation(..., on_thinking: Callable[[str], None]
  | None = None)`, durchgereicht an beide Backends.
- `ClaudeCodeConversation._query()`: `ThinkingBlock`-Inhalte **immer** an
  `on_thinking` statt zu verwerfen. Zusätzlich: `TextBlock`-Inhalte aus
  AssistantMessages, auf die in derselben Runde noch eine
  `ToolUseBlock`-Message folgt, gelten als Zwischenkommentar -> auch an
  `on_thinking`, nicht in `reply`. Nur die `TextBlock`-Inhalte der
  *letzten* AssistantMessage der Runde bilden `reply` (= Antwort-Kanal,
  weiterhin atomar als Ganzes zurückgegeben, siehe core-dialog-loop.md
  Abschnitt 3/7).
- `PiAgentConversation.send()`: `thinking`/`toolCall`/`toolResult`-Blöcke
  aller Events (nicht nur `agent_end`) als lesbarer Text an `on_thinking`;
  `reply` bleibt wie bisher auf die `text`-Blöcke der letzten Message
  beschränkt.
- **Terminal-Sichtbarkeit zentral an einer Stelle**: `on_thinking` wird nicht
  in `llm.py`/`pi_agent.py` selbst geloggt, sondern einmal in `App`s
  Verdrahtung (dort, wo `on_thinking` übergeben wird) -- druckt/loggt den
  Text fürs Terminal **und** hängt ihn ans Chatverlauf-Modell an. Vermeidet,
  dieselbe Logik in beiden Backend-Clients zu duplizieren.
- `App`/`cockpit.py`: Denkprozess-Einträge im Chatverlauf-Modell
  unterscheidbar markieren (z.B. eigenes `role` oder Content-Flag), damit
  `cockpit.py` sie optisch absetzen kann (siehe core-dialog-loop.md
  Abschnitt 3 -- Darstellung selbst, z.B. ob standardmäßig ein- oder
  ausblendbar, ist UI-Detail dieser Oberfläche, nicht Teil dieser Spec).
- Sprachausgabe (`_speak()`) bekommt weiterhin ausschließlich `reply`, nie
  Denkprozess-Inhalte -- das ändert sich durch diesen Umbau nicht, wird aber
  durch die Trennung jetzt tatsächlich sauber eingehalten (Fix des
  Backlog-Bugs "en passant").
- Telegram-Bot: bekommt `on_thinking` vorerst nicht verdrahtet (bleibt bei
  nur der finalen Antwort) -- ob/wie Denkprozess dort später sinnvoll ist,
  ist nicht Teil dieser Spec.

## 4. Stop-Markierung: Umsetzung

- Greift in `App._respond()` (Zweig "Interrupted while thinking --
  discarding reply") und überall, wo `_abort_current_turn()` einen Turn aus
  dem Zustand "thinking" abbricht (`App.stop()`; nicht bei "speaking" --
  dort hat der Antwort-Kanal schon Inhalt, siehe core-dialog-loop.md
  Abschnitt 4, keine Markierung nötig).
- Fügt einen Eintrag in den Chatverlauf ein, der erkennbar kein normaler
  Assistant-Turn ist (z.B. eigener `role` oder Prefix), Inhalt sinngemäß
  "Abgebrochen durch Nutzer". Ein bis zu diesem Zeitpunkt bereits über
  `on_thinking` gesammelter Denkprozess bleibt dabei stehen, wird nicht
  rückwirkend entfernt (siehe Abschnitt 3/core-dialog-loop.md Abschnitt 4).
- `reset()`/`resume_session()` nutzen `_abort_current_turn()` ebenfalls,
  bauen danach aber ohnehin eine neue/andere Historie auf -- die Markierung
  ist dort praktisch irrelevant, soll aber aus Konsistenzgründen (ein
  Primitiv, eine Implementierung) nicht extra unterdrückt werden.

## 5. Akzeptanzkriterien / Testfälle

(Ergänzt die allgemeinen State-Machine-Tests aus der Diskussion zu
core-dialog-loop.md.)

- Fake-`AgentConversation`, die über `on_thinking` zwei Zwischenstücke feuert
  und dann einen finalen String zurückgibt: `_speak()`/der Antwort-Kanal
  bekommt nur den finalen String, nie die Zwischenstücke.
- Dieselbe Fake-Konversation ohne `on_thinking`-Aufrufe (Backend liefert
  keinen Denkprozess): bestehendes Verhalten bleibt unverändert, kein Fehler.
- Stop während "thinking", kein `on_thinking`-Aufruf zuvor: Chatverlauf
  bekommt genau einen neuen Eintrag, die Abbruch-Markierung.
- Stop während "thinking", nachdem `on_thinking` schon Inhalte geliefert
  hat: Denkprozess-Einträge bleiben erhalten, zusätzlich die
  Abbruch-Markierung.
- Stop während "speaking" (Antwort-Kanal war schon vollständig): kein neuer
  Markierungs-Eintrag, Text unverändert, nur TTS wird gestoppt.
- Claude-Pfad: eine AssistantMessage mit Text vor einem Tool-Use, gefolgt von
  der finalen AssistantMessage: nur der Text der letzten Message landet im
  Antwort-Kanal, der vorherige im Denkprozess-Kanal.

## 6. Nicht-Ziele

- Echtes Streaming des Antwort-Kanals selbst (siehe core-dialog-loop.md
  Abschnitt 7) -- bleibt atomar.
- Standardmäßiges Ein-/Ausblenden des Denkprozess-Kanals im Cockpit, Styling
  im Detail -- UI-Entscheidung von `cockpit.py`, nicht hier festgelegt.
- Denkprozess-Anzeige im Telegram-Bot.
