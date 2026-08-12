"""Corrects for ydotool's fixed US-QWERTY keycode table on a German
(QWERTZ) system.

ydotool always maps each ASCII character to the Linux keycode a US
keyboard would use to produce it, then Mutter interprets that raw keycode
through whatever layout is *actually* active (confirmed via `localectl`:
this machine runs the German "de" layout). So e.g. sending 'y' presses the
physical key ydotool thinks means 'y' on a US board -- which sits exactly
where German has 'z', so 'z' appears on screen instead.

This maps each character we want to *appear* on screen to whatever
character must be handed to `ydotool type` so the physical keycode it
ends up pressing is the one German assigns to the desired character. That
also lets us type ae/oe/ue/ss's umlauted forms (ae, oe, ue, ss) correctly
-- ydotool's ASCII table has no keycode for them directly, but they sit on
the same physical keys as ', ;, [ and - respectively, which *are* in its
table.

Table only covers the standard, well-documented DE/US layout deltas.
Rarer symbols (backtick/tilde, the ISO extra key) are left unmapped and
may still come out wrong -- not expected to matter for spoken dictation.
"""

# desired on-screen character -> character to hand to `ydotool type`
_DE_LAYOUT_FIXUP = {
    "y": "z", "Y": "Z",
    "z": "y", "Z": "Y",
    '"': "@",
    "§": "#",
    "&": "^",
    "/": "&",
    "(": "*",
    ")": "(",
    "=": ")",
    "ß": "-",
    "?": "_",
    "ü": "[", "Ü": "{",
    "+": "]",
    "*": "}",
    "#": "\\",
    "'": "|",
    "ö": ";", "Ö": ":",
    "ä": "'", "Ä": '"',
    ";": "<",
    ":": ">",
    "-": "/",
    "_": "?",
}

_TRANSLATION = str.maketrans(_DE_LAYOUT_FIXUP)


def fix_for_de_layout(text: str) -> str:
    return text.translate(_TRANSLATION)
