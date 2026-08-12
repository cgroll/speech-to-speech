"""Standalone system-wide dictation tool (formerly the `parakeet-dictate`
project, folded in here so the STT daemon has exactly one home).

Press a hotkey anywhere, speak, press it again, and the transcribed text is
typed into whatever text field currently has focus -- independent of the
`speech_to_speech` conversational app, sharing only the Parakeet model
choice (see `speech_to_speech.config.STT_MODEL_NAME`).
"""
