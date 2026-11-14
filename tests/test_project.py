"""Persistência: Project -> JSON -> Project, recents, stats e a ponte com o Config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corteia import paths
from corteia.config import Config
from corteia.models import (AnalysisOptions, AspectRatio, Clip, ClipEdit, Codec, CropPlan,
                             ExportJob, ExportSettings, JobState, Project, Resolution, Scene,
                             Segment, Silence, SourceVideo, SubtitleSettings, SubtitleStyle,
                             Transcript, VideoInfo, Word)
from corteia.project import MAX_RECENTS, ProjectStore, Recent

GHOST = "/tmp/este_arquivo_nao_existe_corteia.mp4"


def rich_project() -> Project:
    """Projeto cheio: fonte com transcrição/cenas/silêncio, corte editado e export."""
    p = Project(name="Áudio & Teste", created_at=100.0, updated_at=200.0, notes="notas")
    tr = Transcript("pt", "base", [
        Segment(0.4, 3.6, "Eu larguei um salário alto.",
                [Word(0.4, 1.2, "Eu"), Word(1.2, 3.6, "larguei salário alto.")], 0),
        Segment(4.2, 8.0, "Estabilidade é uma ilusão.",
                [Word(4.2, 6.0, "Estabilidade"), Word(6.0, 8.0, "é ilusão.")], 1),
    ])
    p.sources.append(SourceVideo(
        path=GHOST,
        info=VideoInfo(path=GHOST, duration=14.0, width=1280, height=720,
                       fps=29.97, codec_video="h264", codec_audio="aac", bitrate=7_500_000),
        transcript=tr, scenes=[Scene(0.0, 7.0), Scene(7.0, 14.0)],
        silences=[Silence(4.0, 7.0)], stages_done=["probe", "asr"], origin="file"))
    clip = Clip(source_path=GHOST, start=0.4, end=8.0,
                title="A ilusão da estabilidade", description="desc",
                keywords=["carreira", "renda"], reason="arco completo")
    clip.edit = ClipEdit(aspect=AspectRatio.VERTICAL_9_16,
                         subtitles=SubtitleSettings(style=SubtitleStyle.PROFESSIONAL,
                                                    font="DejaVu Sans", uppercase=False),
                         title_text="TESTE", watermark_text="© corteia",
                         manual_text_edits={0: "Texto corrigido."}, volume=0.85)
    clip.score.signals = {"hook": 0.5}
    p.clips.append(clip)
    p.exports.append(ExportJob(clip_id=clip.id, source_path=GHOST, out_path="/tmp/out.mp4",
                               state=JobState.DONE, started_at=1.0, finished_at=6.5,
                               settings=ExportSettings()))
    return p


def cfg_of(isolated_env) -> Config:
    from corteia import paths as _p
    return Config(_p.data_dir() / "config.json")   # data_dir respeita XDG_CONFIG_HOME


# --------------------------------------------------------------- round trip
from corteia.models import _export_state


def test_round_trip_project_json_project_preserva_tudo():
    p = rich_project()
    d = p.to_dict()
    text = json.dumps(d, ensure_ascii=False)          # tem de ser serializável de fato
    q = Project.from_dict(json.loads(text))
    assert q.to_dict() == d, "a segunda passagem divergiu da primeira"
    assert q.name == "Áudio & Teste" and q.notes == "notas"
    assert q.sources[0].info.width == 1280
    assert q.sources[0].info.fps == pytest.approx(29.97)
    assert q.sources[0].stages_done == ["probe", "asr"]
    assert q.sources[0].transcript.segments[1].words[0].text == "Estabilidade"
    assert q.sources[0].silences[0].duration == pytest.approx(3.0)
    assert len(q.clips) == 1 and q.clips[0].id == p.clips[0].id
    assert q.clips[0].edit.subtitles.style is SubtitleStyle.PROFESSIONAL
    assert q.clips[0].edit.manual_text_edits == {0: "Texto corrigido."}
    assert q.clips[0].edit.volume == pytest.approx(0.85)
    # o histórico é gravado como dict pelo exportador (linhas ricas: título,
    # MB, segundos); ExportJob é só o objeto de fila em memória
    assert _export_state(q.exports[0]) == JobState.DONE.value
    assert json.dumps(q.to_dict(), ensure_ascii=False) == text


def test_enums_e_chaves_inteiras_sobrevivem_ao_json():
    from corteia.project import _job_field

    original = rich_project()
    q = Project.from_dict(json.loads(json.dumps(original.to_dict())))
    assert isinstance(q.options.aspect, AspectRatio)
    assert isinstance(q.export_settings.resolution, Resolution)
    assert isinstance(q.export_settings.codec, Codec)
    assert isinstance(q.clips[0].edit.aspect, AspectRatio)
    assert isinstance(q.clips[0].edit.subtitles, SubtitleSettings)
    # o histórico volta como dict; o tipo de resolução continua o do enum
    assert _job_field(q.exports[0], "settings")["resolution"] == \
        original.exports[0].settings.resolution.value
    # manual_text_edits usa chave int: JSON a transformaria em str se não tratada
    assert list(q.clips[0].edit.manual_text_edits.keys()) == [0]
    assert float(_job_field(q.exports[0], "finished_at")) - float(
        _job_field(q.exports[0], "started_at")) == pytest.approx(5.5)


def test_dataclasses_usam_slots_e_recusam_atributos_inventados():
    plan = CropPlan(points=[(0.0, 10, 20, 100, 200)], mode="face", faces_used=1)
    assert getattr(CropPlan, "__slots__", None) is not None
    assert not hasattr(plan, "__dict__"), "slots=True não deveria criar dict de instância"
    with pytest.raises(AttributeError):
        plan.campo_inventado = 1
    assert plan.to_dict()["mode"] == "face"
    # JSON não tem tupla: points volta como lista de listas, que é o esperado no disco
    assert [tuple(x) for x in CropPlan(**plan.to_dict()).points] == plan.points
    r = Recent("i", "n", "/p.json", 1.0, 2, 3, 1, 14.0, "")
    assert getattr(Recent, "__slots__", None) is not None
    assert r.to_dict()["duration"] == 14.0
    assert Recent.from_dict({**r.to_dict(), "desconhecido": 9}).name == "n"
    assert getattr(Project, "__slots__", None) is not None


def test_chaves_desconhecidas_no_json_nao_derrubam_o_load(isolated_env, tmp_path):
    p = rich_project()
    d = p.to_dict()
    d["fonte_inventada"] = {"x": 1}
    d["clips"][0]["edit"]["nada_aqui"] = 5
    f = tmp_path / "futuro.json"
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    # isolated_env garante que nem recents.json nem stats.json caem no ~/.config real
    store = ProjectStore(cfg_of(isolated_env))
    q = store.load(f)
    assert q is not None and q.clips[0].id == p.clips[0].id
    assert q.name == p.name


def test_status_de_corte_desconhecido_nao_destrui_o_projeto(tmp_path):
    d = rich_project().to_dict()
    d["clips"][0]["status"] = "status_de_um_futuro"
    f = tmp_path / "futuro_status.json"
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    q = Project.from_dict(json.loads(f.read_text(encoding="utf-8")))
    assert q.clips[0].status.value == "suggested"


# --------------------------------------------------------------------- store
def test_store_salva_recarrega_e_indexa_recents(isolated_ctx):
    ctx = isolated_ctx
    p = rich_project()
    ctx.store.current = p
    target = ctx.store.save(p)
    assert Path(target).is_file() and target.parent == paths.projects_dir()
    loaded = ctx.store.load(target)
    assert loaded is not None and loaded.to_dict() == p.to_dict()

    rec = ctx.store.recents()
    assert len(rec) == 1
    assert rec[0].id == p.id and rec[0].clips == 1 and rec[0].exported == 1
    assert rec[0].duration == pytest.approx(14.0)
    assert ctx.store.missing_sources(loaded) == [GHOST]

    ctx.store.delete(target)
    assert not target.exists()
    assert ctx.store.recents() == [], "recent morto tem de sair do índice"


def test_recents_ordenado_do_mais_novo_e_limitado_a_max(isolated_env):
    store = ProjectStore(cfg_of(isolated_env))
    n = MAX_RECENTS + 6
    for i in range(n):
        store.save(Project(name=f"P{i}", id=f"id{i}", created_at=float(i),
                           updated_at=float(i)), store.dir / f"p{i}.json")
    rec = store.recents()
    assert len(rec) == MAX_RECENTS
    assert rec[0].updated_at >= rec[-1].updated_at, "recents tem de vir do mais novo"
    assert all(Path(r.path).exists() for r in rec)
    assert rec[0].name == f"P{n - 1}"


def test_global_stats_soma_o_que_existe_no_disco(isolated_env):
    store = ProjectStore(cfg_of(isolated_env))
    store.save(rich_project(), store.dir / "p.json")
    st = store.global_stats()
    assert st["projects"] == 1
    assert st["videos"] == 1 and st["clips"] == 1 and st["exported"] == 1
    assert st["source_seconds"] == pytest.approx(14.0)
    assert st["data_bytes"] > 0 and st["disk_free_bytes"] > 0
    hist = store.export_history()
    assert len(hist) == 1 and hist[0]["seconds"] == pytest.approx(5.5)
    assert hist[0]["project"] == "Áudio & Teste"


def test_json_corrompido_vira_oserror_e_arquivo_corrupt(isolated_env):
    store = ProjectStore(cfg_of(isolated_env))
    bad = store.dir / "podre.json"
    bad.write_text("{ isso não é json ", encoding="utf-8")
    with pytest.raises(OSError):
        store.load(bad)
    assert not bad.exists()
    assert bad.with_suffix(".corrupt").exists(), "o app preserva o arquivo podre"
    assert store.load(store.dir / "inexistente.json") is None


# ---------------------------------------------------------------- ponte config
def test_ponte_config_para_options_e_export_settings(isolated_env):
    cfg = cfg_of(isolated_env)
    cfg.update({
        "analysis.max_clips": 5, "analysis.min_seconds": 9.0, "analysis.max_seconds": 44.0,
        "analysis.min_score": 33.0, "analysis.aspect": "1:1",
        "analysis.remove_silences": False, "subs.model": "small", "subs.language": "en",
        "export.resolution": "720p", "export.fps": 25, "export.quality_crf": 27,
        "export.codec": "libx265", "export.audio_bitrate": "128k",
    })
    p = ProjectStore(cfg).new("Bridged")
    o: AnalysisOptions = p.options
    assert o.max_clips == 5 and o.min_score == pytest.approx(33.0)
    assert o.target_min == pytest.approx(9.0) and o.target_max == pytest.approx(44.0)
    assert o.aspect is AspectRatio.SQUARE_1_1
    assert o.remove_silences is False and o.whisper_model == "small" and o.language == "en"
    e: ExportSettings = p.export_settings
    assert e.resolution is Resolution.R_720 and e.fps == 25
    assert e.quality_crf == 27 and e.codec is Codec.H265 and e.audio_bitrate == "128k"
    plain = ProjectStore(None).new()
    assert plain.options.max_clips == 12 and plain.name


def test_stats_do_projeto_contam_as_coisas_certas():
    p = rich_project()
    st = p.stats()
    assert st["sources"] == 1 and st["clips"] == 1
    assert st["approved"] == 0 and st["exported"] == 1
    assert p.approved_clips == []
    assert p.find_clip(p.clips[0].id) is p.clips[0]
    assert p.find_clip("fantasma") is None
    assert p.active_source is p.sources[0]


@pytest.fixture()
def store(tmp_config):
    """ProjectStore isolado, com um projeto novo como current."""
    from corteia.project import ProjectStore

    st = ProjectStore(tmp_config)
    st.new("Segurança")
    return st


# --------------------------------------------------------------------------- #
# segurança de dados: um "Abrir projeto" errado nunca pode destruir mídia
# (reprodução real: um vídeo de 28 MB foi renomeado para .corrupt)
# --------------------------------------------------------------------------- #
def test_load_de_video_nao_renomeia_o_arquivo(store, tmp_path):
    video = tmp_path / "material.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x1a" * 4096)
    antes = (video.stat().st_size, video.read_bytes()[:12])
    with pytest.raises(OSError) as exc:
        store.load(video)
    assert "não é um projeto" in str(exc.value)
    assert video.exists() and (video.stat().st_size, video.read_bytes()[:12]) == antes
    assert not (tmp_path / "material.corrupt").exists()


def test_save_nao_escreve_por_cima_de_video(store, tmp_path):
    video = tmp_path / "material.mp4"
    video.write_bytes(b"\x00" * 2048)
    tam = video.stat().st_size
    with pytest.raises(OSError) as exc:
        store.save(store.current, video)
    assert "arquivo de mídia" in str(exc.value)
    assert video.stat().st_size == tam


def test_json_de_projeto_corrompido_continua_quarantenable(store, tmp_path):
    p = tmp_path / "projeto.json"
    p.write_text("nada de json aqui", encoding="utf-8")
    with pytest.raises(OSError):
        store.load(p)
    assert p.exists() is False
    assert (tmp_path / "projeto.corrupt").exists()


def test_extensao_de_video_e_caixa_independente(store, tmp_path):
    up = tmp_path / "FILME.MP4"
    up.write_bytes(b"\x00" * 128)
    with pytest.raises(OSError):
        store.load(up)
    assert up.exists()
