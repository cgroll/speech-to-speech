# Spezifikation: TTS-Ausgabe-Warteschlange und Ordnungsinvariante

Status: Entwurf (2026-10-03) -- Ergänzung zu `speech-extension.md` (Abschnitt
4, "TTS-Daemon Queue", die die dort nur grob beschriebene Serialisierung
ersetzt) und zu `background-channel.md` (Abschnitt 3.4, Sprachausgabe von
Hintergrund-Meldungen). Noch nicht umgesetzt -- reiner Konzeptstand, parallel
zu `background-channel.md` Abschnitt 4 (Status-Abfrage) als offener Punkt.

## 1. Ausgangslage (Befund 2026-10-03)

Es gibt heute **keine echte Warteschlange** für TTS-Ausgaben, nur eine
Belegt-Sperre auf Daemon-Seite:

- `tts_daemon/daemon.py`'s `_claim_speaking()`: Wenn beim Eintreffen eines
  `speak`-Kommandos der Daemon-Zustand nicht `idle` ist, wird sofort
  `{"ok": false, "error": "busy: speaking"}` zurückgegeben -- es wird nichts
  gepuffert oder nachträglich abgespielt.
- `app.py`'s `_speak()` fängt genau diesen Fall (ein `RuntimeError` aus
  `tts_client.speak()`) ab, loggt eine Warnung und behandelt den Turn
  trotzdem als erledigt (`_speak()`'s `except RuntimeError` -- siehe
  Docstring-Kommentar dort zu `DaemonUnavailableError`/"busy: speaking").
  **Das heißt: eine Antwort, die während noch laufender Denkprozess-Audio
  fertig wird, kann komplett unhörbar verworfen werden, nicht nur
  verzögert.**
- Denkprozess-Audio (`App._append_output()` -> `_speak_thinking()`) läuft in
  einem eigenen, verworfenen (`daemon=True`) Thread pro Chunk, ohne
  Koordination mit einem evtl. parallel laufenden `_speak()`-Aufruf für die
  finale Antwort -- beide konkurrieren unabhängig um den einen Belegt-Zustand
  des Daemons. Wer zuerst ankommt, gewinnt; der andere Aufruf scheitert mit
  `busy` und wird verworfen (siehe oben).
- Race-Fenster bei Barge-in: `_append_output()` prüft `_audio_suppressed`
  **einmal**, bevor es den Denkprozess-Audio-Thread startet. `tts_client.
  stop()` (von `on_toggle()`s Barge-in-Pfad aufgerufen) bricht aber nur eine
  beim Daemon **bereits laufende** Wiedergabe ab -- es ist kein dauerhafter
  Riegel. Ein Denkprozess-Chunk, dessen Thread **zwischen** dieser Prüfung
  und seinem tatsächlichen `tts_client.speak()`-Aufruf noch nicht gestartet
  war, kann also nach einem Barge-in trotzdem noch hörbar werden, weil zu dem
  Zeitpunkt, an dem er beim Daemon ankommt, gerade nichts mehr läuft, das
  `stop()` hätte abbrechen können.

Beide Probleme (verworfene Antwort-Audio, durchrutschender Denkprozess nach
Barge-in) sind Symptome derselben fehlenden Abstraktion: einer einzigen,
geordneten Ausgabe-Warteschlange auf `App`-Seite, die den Daemon seriell
bedient, statt mehrerer unabhängiger, konkurrierender Aufrufer.

## 2. Grundprinzip: Ordnungsinvariante zwischen Ein- und Ausgabe

Kernregel (Nutzer-Vorgabe, 2026-10-03): **Die Reihenfolge von Text-Eingaben
und Text-Ausgaben im Chatverlauf muss auch für STT/TTS gelten.** Konkret: Ein
Ausgabe-Eintrag, der im Chatverlauf vor einem Eingabe-Eintrag steht, der
bereits transkribiert und eingereiht wurde, darf nicht mehr nachträglich per
TTS vorgelesen werden -- er ist in dem Moment "veraltet", in dem eine neuere
Nutzereingabe bereits in der Liste steht. Die Antwort bleibt dabei ganz normal
im Text-Chatverlauf nachlesbar; nur die *Sprachausgabe* dieses einen Eintrags
entfällt.

Beispiel (Nutzer-Formulierung): Die Antwort auf Turn 1 ist bereits im
Chatverlauf, wird aber noch (oder noch nicht) vorgelesen. Während dieser
Zeit spricht der Nutzer eine neue Eingabe (Barge-in). Nach STT landet das
Transkript von Turn 2 im Chatverlauf **hinter** der Antwort von Turn 1.
Ab diesem Zeitpunkt ist die Antwort von Turn 1 ordnungs-veraltet -- ihre
Audio-Ausgabe wird nicht mehr gestartet bzw. sofort abgebrochen, falls schon
begonnen.

Das ist eine Verallgemeinerung des heutigen, ad-hoc über `_audio_suppressed`
+ `tts_client.stop()` gelösten Barge-in-Verhaltens (`speech-extension.md`
Abschnitt 3) zu einer einzigen, für alle Ausgabe-Quellen (Antwort,
Denkprozess, Hintergrund-Meldung) gleich angewendeten Regel, statt mehrerer
verstreuter Einzel-Prüfungen (`_audio_suppressed`-Check in `_speak()`,
`_deliver_background_entry()`, `_append_output()`, siehe Abschnitt 1 oben).

Wichtig zur Abgrenzung: Die Invariante gilt nur **Eingabe vor Ausgabe**, nicht
unter Ausgaben selbst. Zwei Denkprozess-Chunks oder ein Denkprozess-Chunk vor
der finalen Antwort sollen weiterhin in Entstehungsreihenfolge abgespielt
werden (FIFO) -- keiner "überholt" den anderen, solange keine neue
Nutzereingabe zwischen sie tritt.

## 3. Die Warteschlange: `_tts_queue` in `App`

Neue Datenstruktur, analog zu `_input_queue`/`_background_queue`, geschützt
durch denselben `_state_lock`:

```
_tts_queue: list[_TtsQueueItem]  # FIFO

@dataclass
class _TtsQueueItem:
    text: str
    done: threading.Event       # vom Worker gesetzt, sobald entschieden/fertig
    played: bool = False        # vom Worker gesetzt, nur True nach echtem Abspielen
    first_chunk_s: float | None = None
    speak_duration_s: float | None = None
```

### 3.1 Korrektur gegenüber dem ursprünglichen Entwurf: keine Sequenznummern

Der erste Entwurf dieses Abschnitts sah eine Sequenznummer pro
`_history`-Eintrag vor, gegen die der Worker beim Dran-Kommen eines Items
prüft, ob zwischenzeitlich eine neuere Nutzereingabe erschienen ist. Beim
Implementieren fiel auf, dass diese Formulierung bei einem **langsam
antwortenden Turn** das Falsche vergleicht: Die `seq` einer Antwort spiegelt
die Stelle wider, an der sie *fertig wird* (spät, wenn das Backend langsam
ist), nicht die Stelle, an der die *auslösende Frage* gestellt wurde -- ein
Vergleich gegen die zuletzt gesehene Nutzereingabe hätte in genau dem Fall,
den die Invariante eigentlich abdecken soll, nichts erkannt. Korrekt wäre
nur ein Vergleich gegen die `seq` der *auslösenden* Eingabe gewesen (zwei
verschiedene Zähler pro Antwort), was zusätzliche Zustands-Weiterleitung
durch `agent_backend.py`/`llm.py`/`pi_agent.py` nötig gemacht hätte.

Das ist in diesem Code aber unnötig: Der Responder-Loop verarbeitet Turns
strikt sequenziell (`_responder_loop_running`-Guard in
`_run_responder_loop()` lässt keinen zweiten Loop zu, solange der erste noch
in `_respond()` steckt) **und** `_respond()` wartet (über den Worker, siehe
3.2/3.3) synchron auf das Ergebnis der eigenen Sprachausgabe, bevor es
zurückkehrt. Eine zweite Nutzereingabe kann ihren Transkriptions-Eintrag in
`_history` also nie bekommen, bevor die Antwort auf die erste bereits
abgespielt oder verworfen wurde -- die Race, die die Sequenznummer fangen
sollte, ist in dieser Architektur für Antworten gar nicht erreichbar.

Für Denkprozess-Chunks und Hintergrund-Meldungen (beide **nicht**
blockierend eingereiht, siehe 3.3) wird dieselbe Garantie stattdessen über
das sofortige Leeren der Warteschlange (Abschnitt 4) plus eine frische
Prüfung von `_audio_suppressed`/Zustand sowohl beim Einreihen als auch beim
tatsächlichen Abspielen erreicht -- einfacher und ohne die oben beschriebene
Fehlerquelle. Sequenznummern sind daher kein Teil der Umsetzung.

### 3.2 Ein einziger Worker-Thread

Ein dauerhafter Worker-Thread (gestartet mit `App.load()`, läuft für die
Lebenszeit des Prozesses, analog zum Responder-Loop-Muster, aber nicht
on-demand gestartet, da er auch Hintergrund-Audio ohne aktiven Turn bedienen
muss) nimmt Einträge aus `_tts_queue` **einzeln und nacheinander**:

1. Eintrag vorne aus der Queue nehmen (blockierend warten, wenn leer, über
   eine `threading.Condition` auf demselben `_state_lock`).
2. **Unmittelbar vor dem tatsächlichen `tts_client.speak()`-Aufruf** (nicht
   schon beim Einreihen, siehe Race-Fenster in Abschnitt 1) frisch prüfen:
   Ist `_audio_suppressed` gesetzt, ist der Zustand `recording`, oder ist
   `_voice_muted` aktiv? Falls ja: Item verwerfen (`played` bleibt `False`),
   `done` setzen, weiter zum nächsten.
3. Sonst: `tts_client.speak(text)` aufrufen (blockiert bis fertig oder durch
   `stop()` abgebrochen), `played`/Zeitwerte setzen, `done` setzen.

Zustandswechsel nach `speaking` passiert weiterhin **beim Einreihen** durch
den jeweiligen Aufrufer (`_respond()`/`_deliver_background_entry()`,
unverändert zu heute) -- nicht erst, wenn der Worker ein Item tatsächlich
abspielt. Nur so wird ein Tastendruck, während mehrere Items noch in der
Queue warten, zuverlässig als Barge-in erkannt (`on_toggle()`s Zustands-Check
`state in ("thinking", "speaking")`) statt fälschlich als Neustart aus
`idle`. Denkprozess-Chunks ändern den Zustand weiterhin nicht (bleibt
`thinking`, unverändert zu heute) -- `on_toggle()`s Barge-in-Zweig deckt
auch `thinking` ab, das genügt.

Damit entfällt die heutige Konkurrenz mehrerer Aufrufer um den Daemon
komplett -- der Daemon selbst bleibt unverändert (serialisiert ohnehin nur
eine Anfrage), aber jetzt schickt ihm immer nur **ein** Aufrufer etwas, in
garantierter Reihenfolge.

### 3.3 Einreihen statt direkt sprechen

`_speak()` und der Sprech-Teil von `_deliver_background_entry()` reihen nur
noch in `_tts_queue` ein und **warten auf das `done`-Event ihres eigenen
Items**, statt selbst blockierend `tts_client.speak()` aufzurufen -- nach
außen (Responder-Loop) weiterhin vollständig synchron, nur die eigentliche
Daemon-Anfrage läuft jetzt über den einen Worker. Denkprozess-Chunks
(`_append_output()`) reihen dagegen **ohne zu warten** ein (reines
Fire-and-forget) -- es gibt dafür keine Turn-Statistik und niemand muss auf
sie warten, bevor der nächste Schritt der laufenden Verarbeitung weiterläuft.

## 4. Barge-in / neue Eingabe: Purge-Regel

Die Pro-Item-Prüfung in Schritt 2 (Abschnitt 3.2) reicht für Korrektheit
bereits aus (veraltete Items würden beim Dran-Kommen einfach übersprungen).
Zusätzlich aber, **beim Eintreffen jeder neuen Nutzereingabe** (in
`_start_recording()` -- deckt sowohl den Idle-Start als auch den
Barge-in-Zweig von `on_toggle()` ab, da beide dort hindurchlaufen -- und in
`submit_text()`, sowie in `_abort_current_turn()` für den Stop-Button):

- `_tts_queue` wird sofort **komplett geleert** (alle wartenden Items
  verworfen, nicht erst beim Dran-Kommen einzeln aussortiert).
- `tts_client.stop()` bricht zusätzlich ein gerade beim Daemon laufendes
  Item ab (unverändert zum heutigen Verhalten).

Das ist eine bewusste Doppelung zur Pro-Item-Prüfung: Ohne sie müsste der
Worker-Thread im schlimmsten Fall durch eine lange Reihe längst veralteter
Denkprozess-Chunks "durchblättern" (jeden einzeln verwerfen), bevor er
wieder etwas Aktuelles abspielt -- harmlos korrekt, aber unnötige Latenz bis
zur nächsten tatsächlich hörbaren Ausgabe. Das sofortige Leeren macht die
Warteschlange nach einem Barge-in wieder augenblicklich leer, statt sie erst
nach und nach "abzuarbeiten".

## 5. Zusammenspiel mit bestehenden Mechanismen

- **`_audio_suppressed`**: bleibt bestehen und wird weiterhin an zwei
  Stellen ausgewertet -- beim Einreihen (günstiger Vorab-Check, vermeidet
  unnötige Queue-Einträge) und verbindlich noch einmal im Worker unmittelbar
  vor dem Abspielen (Schritt 2, Abschnitt 3.2) -- statt wie bisher an drei
  verstreuten, je leicht unterschiedlichen Stellen (`_speak()`,
  `_append_output()`, `_deliver_background_entry()`).
- **Hintergrund-Kanal (`background-channel.md` Abschnitt 3.3)**: Die
  Zustellregeln (sofort bei Idle, nach `_input_queue` bei Thinking/Speaking,
  nie während Recording) betreffen weiterhin, *wann* ein Eintrag in
  `_history`/`_tts_queue` eingereiht wird -- die Ordnungsinvariante aus
  Abschnitt 2 betrifft zusätzlich, *ob* er, einmal eingereiht, später noch
  gesprochen werden darf, falls währenddessen eine neue Eingabe eintrifft.
  Beide Regeln ergänzen sich, ersetzen sich nicht.
- **Denkprozess-Audio**: `thinking-channel-and-stop-marker.md` Abschnitt 3a
  hält noch fest, `_speak()` bekäme "ausschließlich `reply`, nie
  Denkprozess-Inhalte" -- das ist durch die spätere Einführung der
  Denkprozess-Sprachausgabe in `app.py` überholt und sollte bei Gelegenheit dort
  korrigiert werden; dieses Dokument geht vom tatsächlichen Code (Denkprozess
  *wird* gesprochen) aus.

## 6. Akzeptanzkriterien (Entwurf für künftige Tests)

- Eine Denkprozess-Audio wird noch abgespielt, wenn die finale Antwort fast
  gleichzeitig fertig wird: Antwort-Audio wird **nicht** verworfen (kein
  `busy`-Fehler mehr sichtbar), sondern spielt nach dem Denkprozess-Chunk.
- Barge-in während ein Denkprozess-Chunk gerade läuft und ein zweiter Chunk
  bereits in der Queue wartet: Nach dem Barge-in ist die Queue leer, der
  zweite Chunk wird nicht mehr gesprochen, erscheint aber unverändert im
  Text-Chatverlauf.
- Barge-in während mehrere Denkprozess-Chunks einer noch laufenden Antwort
  bereits eingereiht, aber noch nicht gesprochen sind (Antwort selbst noch
  nicht fertig): sofort danach ist `_tts_queue` leer; keiner der wartenden
  Chunks wird nachträglich noch gesprochen, alle bleiben im Text-Chatverlauf
  stehen.
- Zwei Denkprozess-Chunks ohne dazwischenliegende neue Eingabe: beide werden
  in Entstehungsreihenfolge gesprochen, keiner wird durch den anderen
  verworfen.
- Hintergrund-Meldung, eingereiht während Idle, keine neue Eingabe danach:
  wird normal gesprochen (Verhalten unverändert zu `background-channel.md`).

## 7. Nicht-Ziele

- Keine Änderung am TTS-Daemon selbst (`tts_daemon/daemon.py`) -- der bleibt
  bei "eine Anfrage gleichzeitig", die Warteschlange davor löst das
  Konkurrenz-Problem bereits vollständig auf App-Seite.
- Kein Nachholen/Zusammenfassen verworfener Audio-Ausgaben ("hier ist, was du
  verpasst hast") -- wer es lesen will, findet es im Text-Chatverlauf;
  siehe Nutzer-Vorgabe in Abschnitt 2.
- Keine Änderung an der Hintergrund-Kanal-Zustellreihenfolge selbst
  (`background-channel.md` Abschnitt 3.3) -- nur Ergänzung um die
  Sprachausgabe-Gültigkeitsprüfung.
