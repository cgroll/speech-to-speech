# Zustandsmodell der Dialogführung

Das Zustandsmodell von `speech-to-speech` ist in zwei Ebenen unterteilt, um die Kern-Logik von den Besonderheiten des Sprach-Interfaces zu trennen.

## 1. Kern-Text-Modell
Beschreibt die fundamentale Dialogführung, die Abarbeitung der User-Input-Queue und die Generierung von Agenten-Antworten.

👉 **[Spezifikation: Kern-Text-Dialogschleife](core-text-dialog-loop.md)**

## 2. Sprach-Erweiterung (STT/TTS)
Beschreibt die Integration von Spracherkennung und Sprachausgabe, das Verhalten bei Barge-in (Unterbrechung durch den Nutzer) und die Steuerung der Audio-Wiedergabe.

👉 **[Spezifikation: Sprach-Erweiterung (STT/TTS)](speech-extension.md)**

## 3. Hintergrund-Kanal und Status-Abfrage
Beschreibt, wie Ergebnisse von Hintergrund-Agenten (späte, turn-unabhängige
Rückmeldungen) in den Dialog eingespeist werden und wie eine reine
Status-Nachfrage vom normalen Steering/Barge-in abgegrenzt wird.

👉 **[Spezifikation: Hintergrund-Kanal und Status-Abfrage](background-channel.md)**

---

*Diese Seite dient als Einstiegspunkt. Die ursprüngliche, monolithische Spezifikation wurde am 2026-10-02 in diese beiden Teile aufgespalten, um die Queue-Interaktionen klarer darstellen zu können.*
