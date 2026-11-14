"""Background execution helpers for the desktop UI.

The GUI thread must never run FFmpeg, Whisper or any other blocking work, so
every core call goes through :mod:`corteia.workers.jobs` (:class:`JobRunner`).
The package is deliberately free of widget code: it only knows about Qt threads
and the application context signals.
"""
