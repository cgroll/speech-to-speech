# Spezifikation: Hintergrund-Kanal und Status-Abfrage

Status: Abschnitt 3 (Hintergrund-Kanal) umgesetzt (2026-10-03) -- `App.
deliver_background_result()`/`_background_queue` in `app.py` (Zustellregeln
aus 3.3, inklusive Telegram-Variante ohne Leerlauf-Loop in
`telegram_bot/daemon.py`'s `_make_on_background_result()`), gespeist auf der
Claude-Seite durch einen neuen dauerhaften Nachrichten-Dispatcher in
`llm.py` (`_dispatch_loop()`/`_route()`), der die vier `Task*Message`-Typen
aus dem Claude Agent SDK konsumiert statt sie unbeachtet durchzulassen --
siehe `llm.py`'s Moduldocstring. Pi bleibt unberührt (kein eigenes
Hintergrundaufgaben-Primitiv, siehe `agent_backend.create_conversation()`s
Docstring). Tests: `tests/test_background_channel.py`. **Nicht verifiziert:
ob auf der SDK-Verbindung nach der Abschluss-Nachricht einer sichtbaren
Antwort-Runde tatsächlich noch weitere Nachrichten eintreffen, während
sonst nichts läuft** -- nur aus der Dokumentation abgeleitet, noch nicht
gegen eine echte, lange laufende Hintergrundaufgabe getestet.

Abschnitt 4 (Status-Abfrage, `ask_status()`) ist weiterhin nur Entwurf,
nicht umgesetzt -- eigenes, unabhängiges Stück Arbeit.

Ursprünglicher Status: Entwurf (2026-10-02) -- schließt die in
`tests/test_background_and_status_gaps.py` gepinnten Lücken ("Hintergrund-
Agenten / mehr als eine Antwort pro Turn", vormals als Abschnitt 7 in der
inzwischen aufgeteilten `core-dialog-loop.md` markiert). Baut auf
`core-text-dialog-loop.md`, `speech-extension.md`,
`thinking-channel-and-stop-marker.md` und der externen Recherche in
`../reference-claude-code-turn-taking.md` auf.

## 1. Ausgangslage (verifiziert 2026-10-02)

Zwei offene Fragen, bisher nur als Pytest-Baseline dokumentiert, nicht
entschieden:

1. **Hintergrund-Ergebnis ohne laufenden `send()`-Aufruf**: Es gibt aktuell
   keinen sanktionierten Weg, ein Ergebnis von außerhalb des normalen
   Turn-Zyklus in `App` einzuspeisen. Der einzige strukturell mögliche Weg
   heute (`_append_history()` direkt aufrufen) umgeht jede Zustands-Logik --
   ein so eingefügter Eintrag kann vor einer noch ausstehenden echten Antwort
   erscheinen, weil `_history` und `_state_lock` unabhängig voneinander
   geschützt sind.
2. **Status-Nachfrage mitten in der Verarbeitung**: Es gibt keine
   Unterscheidung zwischen "neue Frage" (Steering) und "nur mal nachfragen,
   wie weit du bist" (Peek, soll den laufenden Turn nicht beeinflussen).

Zwischenzeitlich hat sich der Code weiterentwickelt (Barge-in/Steering wurde
umgebaut, siehe `on_toggle()`/`submit_text()` in `app.py`): ein erneuter Lauf
von `test_asking_for_status_mid_processing_is_barge_in_and_discards_the_task`
zeigt, dass die ursprüngliche Annahme "Status-Frage bricht den laufenden Turn
ab" nicht mehr zutrifft (`cancel_calls` bleibt `0`, nicht `1` wie im Test
gepinnt). Heute wird eine Status-Frage still ans Ende der `_input_queue`
gehängt und erst beantwortet, *nachdem* der ursprüngliche Turn fertig ist --
kein Abbruch mehr, aber auch kein echtes "Peek". Gap 2 besteht also weiter,
nur mit anderem Symptom; der Test muss bei Umsetzung dieses Konzepts
aktualisiert werden.

## 2. Grundprinzipien

Übernommen aus der Claude-Code-Recherche, an das eigene Modell angepasst:

- **Queueing statt Abbruch bleibt Standardverhalten** für alles, was wie eine
  normale Nutzereingabe aussieht (bereits so umgesetzt für Barge-in/Steering,
  siehe `speech-extension.md` Abschnitt 3).
- **Push statt Pull für Hintergrund-Ergebnisse** (bewusste Abweichung von
  Claude Codes Pull-Modell, bereits in `reference-claude-code-turn-taking.md`
  Abschnitt 5 entschieden, hier erstmals konkretisiert): ein fertiges
  Hintergrund-Ergebnis soll, wenn das System im Leerlauf ist, aktiv einen
  neuen Mini-Turn anstoßen -- nicht auf die nächste Nutzeraktion warten.
- **Eine einzige Schreibstelle für den Chatverlauf, die mit dem Zustand
  synchron ist**: Hintergrund-Ergebnisse dürfen nicht mehr direkt in
  `_history` geschrieben werden, sondern nur über denselben
  Serialisierungspunkt, der heute schon reguläre Antworten einträgt (der
  Responder-Loop). Behebt Gap 1 strukturell statt per Sonderfall.
- **Status-Abfrage ist kein Turn**: eigene, zustandslose Operation statt
  Überladung von Steering/Barge-in.

## 3. Hintergrund-Kanal

**Klickbare Visualisierung der Zustellregeln aus Abschnitt 3.3** (vierzehn
Schritte in drei Runden: Sofort-Zustellung im Leerlauf, Vorrang einer schon
wartenden Nutzer-Folgefrage, keine Audioausgabe über eine laufende Aufnahme):

<object type="image/svg+xml" data="../diagrams/background-channel-clickable.svg" style="width:100%; max-width:1180px; height:600px;">
</object>

### 3.1 Neue Schnittstelle

`App.deliver_background_result(source: str, text: str) -> None` -- Einstieg
für alles, was nicht aus einem von `App` selbst gestarteten `_llm.send()`
stammt: ein separat laufender Hintergrund-Agent, ein Cloud-Task-Callback
(analog zum Telegram-`reminder`-Mechanismus, der heute bewusst an `App`
vorbeigeht), perspektivisch ein echter Subagenten-Mechanismus. Thread-/
Prozess-sicher aufrufbar.

### 3.2 Warteschlange statt Direktschreiben

Ergebnisse landen zunächst in einer neuen `_background_queue`, geschützt
durch `_state_lock` (analog `_input_queue`) -- **nicht** direkt in
`_history`. Damit gibt es nur noch einen Schreiber, der Zustand und Verlauf
gemeinsam sieht, und die in Gap 1 beobachtete Reihenfolge-Verletzung
(Hintergrund-Eintrag erscheint vor der noch ausstehenden echten Antwort auf
eine frühere Frage) kann strukturell nicht mehr auftreten.

### 3.3 Zustellregeln

| Zustand bei Eintreffen | Zustellzeitpunkt |
| :--- | :--- |
| Idle, Queues leer | Sofort -- `deliver_background_result()` stößt selbst einen Mini-Turn an, da kein Responder-Loop läuft, der sonst pollen würde. |
| Thinking/Speaking | Nach Abarbeitung aller bereits wartenden `_input_queue`-Einträge -- eine gestellte Frage hat Vorrang vor einer unaufgeforderten Meldung. |
| Recording | Erst nachdem die daraus entstehende Aufnahme als eigener Turn durchgelaufen ist -- nie Audio über eine laufende Aufnahme legen. |

Technisch prüft der Responder-Loop `_background_queue` genau dort, wo er
heute schon beim Leerlaufen von `_input_queue` den Zustand auf Idle setzt --
er flusht sie, bevor er sich beendet, statt direkt nach Idle zu wechseln.

### 3.4 Darstellung und Sprachausgabe

Reuse des Denkprozess-Kanal-Musters (`metadata.title`, siehe
`thinking-channel-and-stop-marker.md` Abschnitt 3a): Eintrag bekommt einen
Titel-Präfix aus `source` (z.B. "Build-Agent"), optisch unterscheidbar ohne
neuen `role`-Wert. Sprachausgabe folgt denselben Unterdrückungsregeln wie
jede andere Antwort (`_audio_suppressed`, `_voice_muted`, nie während
`recording`) -- kein Sonderpfad, sondern Wiederverwendung von
`_speak()`/`_set_state("speaking")`, damit Barge-in/Stop während einer
vorgelesenen Hintergrund-Meldung genauso funktionieren wie bei jeder anderen
Antwort.

### 3.5 Transport (bewusst offen)

Wie das Ergebnis physisch zu `deliver_background_result()` gelangt, ist eine
separate Entscheidung: In-Process-Callback, wenn `App` den Hintergrund-Agent
selbst gestartet hat, oder ein kleiner lokaler Endpunkt (Socket/HTTP, analog
zum bestehenden STT-/TTS-Daemon-Muster) für externe Prozesse. Nicht Teil
dieses Konzepts.

## 4. Status-Abfrage

### 4.1 Design: eigene, zustandslose Operation

Neue Methode `App.ask_status() -> str`, die **nicht** über `_llm`/
`_input_queue` läuft, sondern lokal eine kurze Zusammenfassung zusammensetzt
und vorliest: aktueller `_state`, verstrichene Zeit seit Turn-Start, jüngster
bereits via `on_output`/Denkprozess-Kanal bekannter Zwischenstand (steht
schon als `CATEGORY_THINKING`-Eintrag in `_history`). Kein `cancel()`, keine
Zustandsänderung, kein Eintrag in `_input_queue`.

### 4.2 Auslösung (Input-Schicht, nicht Teil des Zustandsmodells)

Entscheidend: der gesprochene Inhalt ist erst **nach** der Transkription
bekannt -- zu dem Zeitpunkt hat eine Aufnahme aber schon begonnen und ggf.
schon Audio-Suppression ausgelöst. Eine Status-Abfrage kann sich also nicht
per Spracherkennung vom normalen Steering unterscheiden, ohne dass Aufnahme/
Unterbrechung längst passiert sind. Zwei Optionen, beide auf Input-Ebene statt
im Zustandsmodell:

- **Cockpit**: dedizierter "Status"-Button (analog zum bestehenden
  "Stop"-Button), ruft `ask_status()` direkt auf.
- **Sprachsteuerung**: inhaltsunabhängiger Trigger (z.B. langer vs. kurzer
  Tastendruck am Jabra/Hotkey) statt Erkennung über transkribierten Text.

## 5. Zusammenspiel mit TTS-/STT-Warteschlangen

- Hintergrund-Zustellung läuft über dieselbe TTS-Pipeline wie reguläre
  Antworten -> Barge-in/Stop wirken unverändert.
- `ask_status()` liefert eine kurze, sofortige Antwort, ohne eine laufende
  TTS-Wiedergabe (regulär oder Hintergrund) zu blockieren -- wie der
  TTS-Daemon zwei gleichzeitige Anfragen priorisiert, ist ein Detail der
  Daemon-Queue (`speech-extension.md` Abschnitt 4), hier nur als Anforderung
  benannt.
- `_background_queue` bleibt strikt getrennt von `_input_queue`: eine
  Transkription darf nie als Hintergrund-Ergebnis interpretiert werden und
  umgekehrt.

## 6. Akzeptanzkriterien (Entwurf für künftige Tests)

- Hintergrund-Ergebnis trifft während Idle ein -> sofort gesprochen/
  angezeigt, ohne manuellen Trigger.
- Hintergrund-Ergebnis trifft während Thinking ein, mit bereits wartender
  User-Eingabe in `_input_queue` -> die User-Eingabe wird zuerst beantwortet,
  die Hintergrund-Meldung danach.
- Hintergrund-Ergebnis trifft während Recording ein -> keine Audioausgabe,
  solange die Aufnahme läuft; Zustellung erst nach dem daraus entstehenden
  Turn.
- `ask_status()` während Thinking liefert eine Antwort, ohne den laufenden
  Turn zu beeinflussen (`_llm`/`_input_queue` unverändert, kein `cancel()`).
- Zwei Hintergrund-Ergebnisse, die während desselben Turns eintreffen, werden
  gebündelt zugestellt, nicht einzeln nacheinander (siehe Claude Codes
  Bündelungsverhalten, `reference-claude-code-turn-taking.md` Abschnitt 5).

## 7. Nicht-Ziele

- Kein generisches Subagenten-Framework -- nur der Zustellmechanismus für
  bereits fertige Texte.
- Kein Streaming der Hintergrund-Antwort selbst (bleibt atomar, wie der
  reguläre Antwort-Kanal, siehe `thinking-channel-and-stop-marker.md`
  Abschnitt 6).
- Korrelation einer Hintergrund-Meldung zu einer bestimmten früheren
  Nutzeranfrage (Turn-ID-Tracking) -- für den aktuellen Anwendungsfall (eine
  Quelle, lose gekoppelt) nicht nötig; das `source`-Label im Titel reicht.
