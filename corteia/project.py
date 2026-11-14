"""Project persistence: one JSON file per project plus a recents index.

Layout under the data dir::

    projects/<id>.json   full project state (sources, clips, transcripts,
                         face tracks, crop plans, export history)
    recents.json         ordered index for the dashboard
    stats.json           global counters

Transcripts and face tracks are stored inline so a project can be resumed even
when the source video is temporarily offline (analysis needs no video; rendering
does, and the app says so instead of failing cryptically).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import paths
from .models import ExportJob, JobState, Project

MAX_RECENTS = 24


@dataclass(slots=True)
class Recent:
    id: str
    name: str
    path: str
    updated_at: float
    sources: int
    clips: int
    exported: int
    duration: float
    thumb: str

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Recent":
        known = {k: v for k, v in d.items() if k in Recent.__dataclass_fields__}
        return Recent(**known)


def _job_field(job: object, name: str, default: object = None) -> object:
    """Lê um campo de linha de histórico seja ``dict`` seja ``ExportJob``.

    O exportador grava dicts (com ``out``/``at``/``seconds``); ``ExportJob`` usa
    ``out_path``/``finished_at``.  Os dois chegam aqui e um ``AttributeError``
    nesta rota quebrava o painel e o histórico inteiro da interface.
    """
    if isinstance(job, dict):
        return job.get(name, default)
    return getattr(job, name, default)


def _job_done(job: object) -> bool:
    state = _job_field(job, "state", "")
    return str(getattr(state, "value", state)) == "done"


def _job_seconds(job: object) -> float:
    direto = _job_field(job, "seconds", None)
    if direto is not None:
        try:
            return max(0.0, float(direto))
        except (TypeError, ValueError):
            return 0.0
    fim = float(_job_field(job, "finished_at", 0.0) or 0.0)
    inicio = float(_job_field(job, "started_at", 0.0) or 0.0)
    return max(0.0, fim - inicio)


#: extensões que NUNCA são projetos: são o trabalho do usuário. Um "Abrir
#: projeto" apontando para um .mp4 (erro de digitação, filtro ignorado, drag &
#: drop no lugar errado) não pode, sob nenhuma hipótese, renomear ou sobrescrever
#: o arquivo -- foi exatamente assim que um vídeo de 28 MB virou ".corrupt".
MEDIA_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg",
              ".ts", ".wmv", ".flv", ".mp3", ".wav", ".m4a", ".aac", ".flac",
              ".ogg", ".opus", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ass",
              ".srt", ".vtt", ".pdf", ".zip", ".iso", ".exe", ".dll"}
#: um projeto legítimo é JSON pequeno; nada perto disso é movido
PROJECT_MAX_BYTES = 64 * 1024 * 1024


def _is_project_file(p: Path) -> bool:
    """Só um JSON pequeno de projeto pode ser posto de lado (.corrupt)."""
    if p.suffix.lower() in MEDIA_EXTS:
        return False
    if p.suffix.lower() != ".json":
        return False
    try:
        return p.stat().st_size <= PROJECT_MAX_BYTES
    except OSError:
        return False


class ProjectStore:
    def __init__(self, config=None) -> None:
        self.config = config
        self.dir = paths.projects_dir()
        self.current = Project(created_at=time.time(), updated_at=time.time())
        self._current_path: Path | None = None

    # ------------------------------------------------------------- file io
    def _file(self, project_id: str) -> Path:
        return self.dir / f"{project_id}.json"

    def save(self, project: Project, path: Path | None = None) -> Path:
        target = path or (self._current_path or self._file(project.id))
        if Path(target).suffix.lower() in MEDIA_EXTS:
            # save() troca o arquivo no lugar (tmp -> target). Apontado para um
            # vídeo, ele destruiria o material original do usuário.
            raise OSError(f"Não vou salvar um projeto sobre '{Path(target).name}': "
                          "é um arquivo de mídia. Escolha um nome .corteia.json.")
        project.updated_at = time.time()
        payload = project.to_dict()
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(target)
        self._index(project, target)
        self._accumulate_stats(project)
        return target

    def save_current(self) -> Path:
        return self.save(self.current)

    def load(self, path: Path | str) -> Project | None:
        p = Path(path)
        if not p.exists():
            self._drop_recent(str(p))
            return None
        if p.suffix.lower() in MEDIA_EXTS:
            raise OSError(f"'{p.name}' não é um projeto do CorteIA "
                          f"(é {p.suffix or 'sem extensão'}). Use Importar para "
                          "abrir vídeos.")
        try:
            project = Project.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Um projeto realmente corrompido é posto de lado (.corrupt) para
            # que o "Abrir" seguinte não trave de novo. Qualquer outra coisa --
            # vídeo, áudio, imagem, JSON grande, arquivo de outro programa --
            # é apenas rejeitada, sem tocar no disco.
            if not _is_project_file(p):
                raise OSError(f"'{p.name}' não é um projeto válido ({exc}). "
                              "O arquivo não foi modificado.") from exc
            corrupt = p.with_suffix(".corrupt")
            try:
                p.replace(corrupt)
            except OSError:
                pass
            raise OSError(f"Projeto corrompido: {p} ({exc}). Arquivo movido para {corrupt.name}") from exc
        self.current = project
        self._current_path = p
        self._index(project, p)
        return project

    def new(self, name: str = "") -> Project:
        proj = Project(
            name=name or f"Projeto {time.strftime('%d/%m %H:%M')}",
            created_at=time.time(), updated_at=time.time(),
        )
        if self.config is not None:
            from .models import AnalysisOptions, ExportSettings
            proj.options = AnalysisOptions.from_dict(_options_from_config(self.config))
            proj.export_settings = ExportSettings.from_dict(_export_from_config(self.config))
        self.current = proj
        self._current_path = None
        return proj

    def open_recent_or_new(self) -> Project | None:
        recents = self.recents()
        return self.load(recents[0].path) if recents else None

    def delete(self, path: Path | str) -> None:
        p = Path(path)
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
        self._drop_recent(str(p))

    # ------------------------------------------------------------- recents
    def recents(self) -> list[Recent]:
        idx = self._read(paths.data_dir() / "recents.json")
        out: list[Recent] = []
        for item in idx.get("items", []):
            try:
                r = Recent.from_dict(item)
            except (KeyError, TypeError):
                continue
            if Path(r.path).exists():
                out.append(r)
        out.sort(key=lambda r: r.updated_at, reverse=True)
        return out[:MAX_RECENTS]

    def _index(self, project: Project, path: Path) -> None:
        idx_file = paths.data_dir() / "recents.json"
        idx = self._read(idx_file)
        items = [i for i in idx.get("items", []) if i.get("path") != str(path)]
        st = project.stats()
        thumb = ""
        for s in project.sources:
            if s.thumb and Path(s.thumb).exists():
                thumb = s.thumb
                break
        items.insert(0, Recent(
            id=project.id, name=project.name, path=str(path), updated_at=project.updated_at,
            sources=st["sources"], clips=st["clips"], exported=st["exported"],
            duration=sum((s.info.duration if s.info else 0.0) for s in project.sources),
            thumb=thumb,
        ).to_dict())
        idx["items"] = items[:MAX_RECENTS]
        self._write(idx_file, idx)

    def _drop_recent(self, path: str) -> None:
        idx_file = paths.data_dir() / "recents.json"
        idx = self._read(idx_file)
        idx["items"] = [i for i in idx.get("items", []) if i.get("path") != path]
        self._write(idx_file, idx)

    # --------------------------------------------------------------- stats
    def global_stats(self) -> dict:
        """Counters recomputed from disk so the dashboard cannot drift from reality."""
        stats = self._read(paths.data_dir() / "stats.json")
        recents = self.recents()
        return {
            "projects": len(recents),
            "videos": max(int(stats.get("videos", 0)), sum(r.sources for r in recents)),
            "clips": max(int(stats.get("clips", 0)), sum(r.clips for r in recents)),
            "exported": max(int(stats.get("exported", 0)), sum(r.exported for r in recents)),
            "source_seconds": sum(r.duration for r in recents),
            "temp_bytes": paths.temp_usage_bytes(),
            "data_bytes": paths.projects_usage_bytes(),
            "disk_free_bytes": paths.free_bytes_temp(),
        }

    def _accumulate_stats(self, project: Project) -> None:
        f = paths.data_dir() / "stats.json"
        stats = self._read(f)
        stats["videos"] = max(int(stats.get("videos", 0)), len(project.sources))
        stats["clips"] = max(int(stats.get("clips", 0)), len(project.clips))
        stats["exported"] = sum(1 for e in project.exports if _job_done(e))
        stats["last_run"] = time.time()
        self._write(f, stats)

    def export_history(self, limit: int = 60) -> list[dict]:
        rows: list[dict] = []
        for r in self.recents():
            try:
                project = Project.from_dict(json.loads(Path(r.path).read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            for job in project.exports:
                if not _job_done(job):
                    continue
                rows.append({
                    "project": project.name,
                    "clip_id": str(_job_field(job, "clip_id", "")),
                    "path": str(_job_field(job, "out_path", "")
                                or _job_field(job, "out", "") or ""),
                    "finished_at": float(_job_field(job, "finished_at", 0.0)
                                         or _job_field(job, "at", 0.0) or 0.0),
                    "seconds": _job_seconds(job),
                })
        rows.sort(key=lambda x: x["finished_at"], reverse=True)
        return rows[:limit]

    def missing_sources(self, project: Project) -> list[str]:
        return [s.path for s in project.sources if s.path and not Path(s.path).exists()]

    # ---------------------------------------------------------------- raw
    @staticmethod
    def _read(path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass


def _options_from_config(cfg) -> dict:
    return {
        "target_min": cfg.float_("analysis.min_seconds"),
        "target_max": cfg.float_("analysis.max_seconds"),
        "preset_duration": cfg.int_("analysis.duration_preset"),
        "max_clips": cfg.int_("analysis.max_clips"),
        "min_score": cfg.float_("analysis.min_score"),
        "aspect": cfg.str_("analysis.aspect") or "9:16",
        "subtitle_style": cfg.str_("subs.default_style") or "viral",
        "remove_silences": cfg.bool_("analysis.remove_silences"),
        "smart_boundaries": cfg.bool_("analysis.smart_boundaries"),
        "follow_speaker": cfg.bool_("analysis.follow_speaker"),
        "use_llm": cfg.str_("api.provider") not in ("", "off"),
        "whisper_model": cfg.str_("subs.model") or "base",
        "language": cfg.str_("subs.language") or "pt",
    }


def _export_from_config(cfg) -> dict:
    return {
        "codec": cfg.str_("export.codec") or "libx264",
        "resolution": cfg.str_("export.resolution") or "1080p",
        "fps": cfg.int_("export.fps") or 30,
        "quality_crf": cfg.int_("export.quality_crf") or 20,
        "preset": cfg.str_("export.preset") or "fast",
        "audio_bitrate": cfg.str_("export.audio_bitrate") or "192k",
        "aspect": cfg.str_("analysis.aspect") or "9:16",
        "out_dir": cfg.str_("export.dir"),
        "filename_pattern": cfg.str_("export.pattern") or "{index:02d}-{title}-corteia",
        "max_parallel": cfg.int_("proc.max_parallel_jobs") or 2,
    }
