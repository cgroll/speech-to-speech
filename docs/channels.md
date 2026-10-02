# Kanäle und Output-Steuerung

`speech-to-speech` unterstützt verschiedene Ein- und Ausgabekanäle. Diese Seite gibt eine Übersicht darüber, wie Text, Audio und Metadaten (Denkprozess, Tools) in den jeweiligen Kanälen verarbeitet werden.

## 1. Kanal: Desktop (Gradio + STT/TTS)

Dies ist das primäre Interface für die lokale Nutzung.

- **Eingabe**:
    - **Sprache**: Über Jabra-Headset oder Hotkey. Das Transkript wird in die `User-Input-Queue` eingereiht.
    - **Text**: Über das Textfeld im Web-Cockpit. Wird sofort im Chatverlauf angezeigt und in die Queue eingereiht.
- **Ausgabe**:
    - **Text (Verlauf)**: Zeigt den gesamten Dialog an.
    - **Audio (TTS)**: Die Antwort des Agenten wird über die lokalen Lautsprecher ausgegeben.
    - **Metadaten**: Denkprozesse (`thinking`) werden als einklappbare Blasen angezeigt. Tool-Aufrufe (`other`) werden aktuell gefiltert (siehe unten), um das UI nicht zu überladen.
- **Stummschaltung**: Über die Checkbox "Audio-Ausgabe stumm" kann die TTS-Ausgabe deaktiviert werden. Der Text-Dialog läuft unverändert weiter.

## 2. Kanal: Telegram (Bot)

Ermöglicht die Nutzung von unterwegs.

- **Eingabe**:
    - **Sprache**: Als Sprachnachricht. Wird transkribiert und das Transkript als Text-Echo zurückgesendet, bevor es an den Agenten geht.
    - **Text**: Als normale Textnachricht.
- **Ausgabe**:
    - **Text**: Die Antwort des Agenten kommt als Textnachricht zurück.
    - **Audio (TTS)**: Optional über den Befehl `/voice audio`. Wenn aktiviert, wird die Antwort zusätzlich als Sprachnachricht gesendet.
    - **Metadaten**: Vereinzelte Denkprozesse (`thinking`) können als Text-Präfix angezeigt werden. Tool-Aufrufe werden auch hier gefiltert.

## 3. Output-Steuerung nach Modus

Das System unterscheidet primär zwischen den Oberflächen **Gradio** (lokales Cockpit) und **Telegram** (Remote). In beiden kann die Sprachausgabe (Voice) an- oder ausgeschaltet werden.

| Output-Kategorie | Oberfläche | Text-Anzeige | Audio (Voice ON) | Audio (Voice OFF) |
| :--- | :--- | :--- | :--- | :--- |
| **Antwort** | Gradio | ✅ | ✅ | ❌ |
| | Telegram | ✅ | ✅ (Voice Message) | ❌ |
| **Denkprozess** | Gradio | ✅ (Blase) | ✅ | ❌ |
| | Telegram | ✅ (Präfix) | ❌ | ❌ |
| **Tools / Other** | Gradio | ❌ (Terminal) | ❌ | ❌ |
| | Telegram | ❌ | ❌ | ❌ |

- **Text-Anzeige**: Findet immer statt (außer bei Tools, die zur Übersichtlichkeit gefiltert werden).
- **Audio (Voice ON)**: In Gradio wird die Audio-Ausgabe direkt über lokale Lautsprecher ausgegeben. In Telegram wird zusätzlich zur Text-Antwort eine Sprachnachricht gesendet.
- **Tools**: Tool-Aufrufe und Ergebnisse sind für Entwickler weiterhin im Terminal sichtbar, werden aber in den Benutzer-Oberflächen ausgeblendet.

## 4. Input-Separation

Alle Eingaben werden als separate Nachrichten behandelt. Wenn ein Nutzer mehrere Nachrichten schnell hintereinander sendet (oder tippt, während der Agent noch denkt), werden diese nacheinander abgearbeitet. 

- Im **Chatverlauf** (Gradio & Telegram) erscheinen diese als getrennte Sprechblasen.
- Im **Agenten-Kontext** werden sie als aufeinanderfolgende User-Turns wahrgenommen, was die Konsistenz der Konversation wahrt.
