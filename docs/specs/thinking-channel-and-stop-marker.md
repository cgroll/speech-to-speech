# Spezifikation: Denkprozess-Kanal und Stop-Markierung

Status: Teilweise umgesetzt (2026-10-02) -- Abschnitt 3 (Denkprozess-/
Output-Kanal) ist implementiert und empirisch gegen beide echten Backends
verifiziert, siehe Abschnitt 3a. Abschnitt 4 (Stop-Markierung im
Chatverlauf) ist weiterhin nur Entwurf, noch nicht gebaut, und wurde
durch das Steering-Message-Modell (kein Abbruch bei Barge-in) teilweise
überholt.

Konkretisiert zwei Lücken, die beim Abgleich von
`docs/specs/core-dialog-loop.md` gegen den aktuellen Code aufgefallen sind:
den fehlenden Denkprozess-Kanal und die fehlende Markierung im Chatverlauf,
wenn ein Stop ohne Antwort endet. Baut direkt auf den dortigen Entscheidungen
auf (Abschnitt 3 "Ausgabe-Kanäle", Abschnitt 4 "Stop").

## 1. Ist-Zustand (Befund 2026-10-02, vor Umsetzung -- siehe Abschnitt 3a für
den aktuellen Stand)

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

## 3. Denkprozess-Kanal: Schnittstellen-Entwurf (ursprünglich, teils überholt)

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
  Backlog-Bugs "en passant"). **Überholt:** Mit der später eingeführten
  Denkprozess-Sprachausgabe (`docs/specs/tts-output-queue.md`) wird
  Denkprozess-Text inzwischen bewusst über dieselbe geordnete TTS-Warteschlange
  gesprochen wie `reply` -- dieser Punkt beschreibt nur den Stand von
  2026-10-02, nicht mehr den aktuellen Code.
- Telegram-Bot: bekommt `on_thinking` vorerst nicht verdrahtet (bleibt bei
  nur der finalen Antwort) -- ob/wie Denkprozess dort später sinnvoll ist,
  ist nicht Teil dieser Spec.

## 3a. Tatsächliche Umsetzung (2026-10-02)

Beim Bauen auf drei statt zwei Kanäle verallgemeinert, auf Wunsch aus der
Diskussion zu möglichen Erweiterungen des Kern-Modells: **Antwort**
(unverändert `send()`s Rückgabewert, atomar), **Denkprozess**
(`agent_backend.CATEGORY_THINKING`) und **Sonstiges**
(`agent_backend.CATEGORY_OTHER`) als bewusster Auffangkorb für alles, was
noch nicht einzeln benannt ist -- aktuell Tool-Aufrufe/-Ergebnisse, aber
offen für künftige, heute unbekannte Blocktypen (z.B. die Claude-SDK-eigenen
`Task*`-Message-Typen, die bei der Recherche zu dieser Erweiterung auffielen
-- vermutlich mit den noch offenen Hintergrund-Agenten-Fragen aus
core-dialog-loop.md Abschnitt 7 verwandt, hier aber nicht weiter verfolgt).

- `agent_backend.py`: `CATEGORY_THINKING`/`CATEGORY_OTHER`-Konstanten;
  `create_conversation(..., on_output: Callable[[str, str], None] | None =
  None)` (Signatur `(kategorie, text)`, nicht nur `(text)` wie im
  ursprünglichen `on_thinking`-Entwurf oben) an beide Backends durchgereicht.
- `llm.py`, `ClaudeCodeConversation._query()`: klassifiziert jeden Block
  live beim Eintreffen -- `ThinkingBlock` -> `CATEGORY_THINKING`,
  `ToolUseBlock` (auf der `AssistantMessage`) und `ToolResultBlock` (kommt
  als eigene `UserMessage` zurück, nicht auf der anfragenden
  `AssistantMessage` -- beim Umsetzen festgestellt, im ursprünglichen
  Entwurf oben nicht erwähnt) -> `CATEGORY_OTHER`. Die `TextBlock`-Inhalte
  bilden die Antwort per Ein-Nachricht-Vorausschau: provisorisch die
  Antwort, bis eine *weitere* `AssistantMessage` mit Text folgt -- dann wird
  der vorherige Text nachträglich als `CATEGORY_THINKING` nachgereicht und
  der neue ist jetzt provisorisch die Antwort. Was am Ende noch offen ist,
  ist die tatsächliche Antwort (ersetzt die reine "nur letzte Message
  zählt"-Regel durch dieselbe Regel, nur jetzt live statt erst im
  Nachhinein ausgewertet).
- `pi_agent.py`, `PiAgentConversation.send()`: analog, aber über
  `message_end`-Events (nicht erst `agent_end`) -- empirisch gegen das
  echte `pi --mode json`-Event-Schema verifiziert (Event-Typen wie
  `message_start`/`message_update`/`message_end`/`tool_execution_*`/
  `turn_end`, nicht im ursprünglichen Entwurf oben bekannt). `thinking`-
  und `toolCall`-Blöcke einer Assistant-`message_end` sowie `toolResult`-
  Messages gehen sofort an `on_output`; Text-Blöcke laufen durch dieselbe
  Ein-Nachricht-Vorausschau wie beim Claude-Pfad.
- `App._append_output()` (statt generischem "role/Content-Flag" wie oben
  skizziert): nutzt Gradio 6's eingebautes `ChatMessage.metadata`-Feld
  (`{"title": ..., "status": "done"}`) -- `gr.Chatbot` rendert das von
  Haus aus als eigene, farblich/optisch abgesetzte, einklappbare
  "Gedanken"-Blase, ganz ohne eigenes CSS oder eigenen `role`-Wert (per
  Gradio-Quellcode und isoliertem `Chatbot.postprocess()`-Aufruf
  verifiziert). `_speak()` bekommt unverändert nur `reply`, nie
  `on_output`-Inhalte -- strukturell gar nicht anders verdrahtet. **Überholt
  (siehe Korrektur oben):** `on_output`-Inhalte der Kategorie Denkprozess
  werden seit `docs/specs/tts-output-queue.md` zusätzlich separat an die TTS-
  Warteschlange gegeben (`App._append_output()` -> `_enqueue_tts()`) -- nicht
  über `_speak()`, aber eben doch hörbar, nicht mehr nur Text.
- Terminal-Logging wie oben geplant **nicht** umgesetzt (nur
  Chatverlauf/Gradio) -- bisher kein Bedarf.
- Telegram-Bot: weiterhin nicht verdrahtet, wie oben entschieden. Das
  Plumbing (`on_output`) ist aber jetzt da und backend-seitig fertig; nur
  `telegram_bot/daemon.py` müsste es noch an `create_conversation()`
  übergeben und selbst entscheiden, wie es Denkprozess-Nachrichten in
  Telegram darstellt (eigene Nachricht? Präfix?) -- offen.
- Verifikation: kein Fake-Backend-Pytest für die Block-Klassifizierung
  selbst (bräuchte einen vollen Mock des Claude-SDK-Async-Clients bzw. der
  `pi`-Subprocess-Ausgabe) -- stattdessen zwei echte End-to-End-Smoke-Tests
  gegen beide laufenden Backends ("liste Dateien auf, dann sag Hallo"),
  die bestätigen: Tool-Aufruf/-Ergebnis landen in `CATEGORY_OTHER`, der
  finale Gruß ist exakt und ausschließlich der `send()`-Rückgabewert.

## 4. Stop-Markierung: Umsetzung (Entwurf)

- Greift nur beim **dedizierten Stop-Button** (`App.stop()`), wenn dieser
  einen Turn aus dem Zustand "thinking" hart abbricht.
- Ein **Barge-in** (Tastendruck während Processing) führt im aktuellen
  Steering-Modell **nicht** zu einer Abbruch-Markierung, da der Turn im
  Hintergrund zu Ende geführt wird und die Antwort im Chat erscheint.
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
  im Detail -- UI-Entscheidung von `cockpit.py`, nicht hier festgelegt
  (in der Umsetzung, Abschnitt 3a: Gradios Standard-Darstellung für
  `metadata`-Nachrichten übernommen, nicht weiter angepasst).
- Denkprozess-Anzeige im Telegram-Bot -- Callback-seitig vorbereitet
  (Abschnitt 3a), in `telegram_bot/daemon.py` aber nicht verdrahtet.
