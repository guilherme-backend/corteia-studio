"""Exception hierarchy for the processing core.

Every failure raised out of ``corteia.core`` is a :class:`CorteiaError`, so the UI
only has to catch one type. ``message`` is always user-facing Brazilian Portuguese;
``detail`` keeps the technical text (FFmpeg stderr, original exception) for the log.
"""

from __future__ import annotations

from typing import Any

# Stage names accepted by ProcessingError (models.Stage values plus core stages).
_KNOWN_STAGES = {
    "import", "probe", "transcribe", "scenes", "silence", "analyze", "face",
    "render", "export", "done", "ffmpeg", "ffprobe", "media", "download",
    "model", "process",
}


def _is_stage(value: str) -> bool:
    return "_" not in value and "." not in value and " " not in value and value.lower() in _KNOWN_STAGES


class CorteiaError(Exception):
    """Base class for every error raised by the application core."""

    def __init__(self, message: str = "Ocorreu um erro inesperado.") -> None:
        super().__init__(message)
        self.message = message


class CancelledError(CorteiaError):
    """Raised when the user cancelled an operation (never shown as an error)."""

    def __init__(self, message: str = "Operação cancelada pelo usuário.") -> None:
        super().__init__(message)


class ProcessingError(CorteiaError):
    """A stage failed. ``stage`` names it, ``message`` explains it in Portuguese.

    The constructor is tolerant: ``ProcessingError("O arquivo está corrompido")``
    is also accepted and stored as the message with the class' default stage, so
    callers can never accidentally hide the text the UI has to show.
    """

    default_stage = "process"

    def __init__(self, stage: Any = None, message: str = "", detail: str = "") -> None:
        stage_str = str(getattr(stage, "value", stage)) if stage is not None else ""
        if not message:
            # single-argument form: decide whether the caller meant a stage or a message
            if stage_str and not _is_stage(stage_str):
                stage_str, message = self.default_stage, stage_str
        if not stage_str:
            stage_str = self.default_stage
        super().__init__(message or f"Falha na etapa '{stage_str}'.")
        self.stage = stage_str
        self.message = message or f"Falha na etapa '{stage_str}'."
        self.detail = str(detail or "")

    @property
    def full(self) -> str:
        """Message plus technical detail, for the log file."""
        return f"{self.message}\n{self.detail}".strip() if self.detail else self.message

    def __str__(self) -> str:
        return self.full


class MediaError(ProcessingError):
    """Invalid/unsupported media or a failed FFmpeg/ffprobe invocation."""

    default_stage = "media"


class TranscriptionError(ProcessingError):
    """Speech recognition failed or produced nothing usable."""

    default_stage = "transcribe"


class RenderError(ProcessingError):
    """Clip rendering/exporting failed."""

    default_stage = "render"


class ModelMissingError(ProcessingError):
    """A required model is not on disk (or the download was impossible)."""

    default_stage = "model"

    def __init__(self, stage: Any = None, message: str = "", detail: str = "",
                 hint: str = "") -> None:
        super().__init__(stage, message, detail)
        self.hint = str(hint) or "Baixe o modelo em Configurações → Modelos e tente novamente."


class FFmpegMissingError(CorteiaError):
    """No usable ffmpeg/ffprobe binary was found on the system."""

    def __init__(self, message: str = "FFmpeg não encontrado. Instale o FFmpeg ou informe o "
                                      "caminho em Configurações → FFmpeg.") -> None:
        super().__init__(message)
