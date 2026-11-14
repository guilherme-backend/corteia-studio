"""Sonda de mídia real: probe, quadros, áudio e curva de energia via FFmpeg."""

from __future__ import annotations

from pathlib import Path

import cv2
import pytest

from corteia.core import media
from corteia.core.errors import MediaError
from corteia.models import VideoInfo

from conftest import ffprobe_json

pytestmark = pytest.mark.ffmpeg


def test_probe_le_as_dimensoes_e_codecs_de_fato(sample_video):
    info = media.probe(sample_video)
    assert isinstance(info, VideoInfo)
    assert (info.width, info.height) == (1280, 720)
    assert abs(info.fps - 30.0) < 0.01
    assert 13.5 <= info.duration <= 14.5
    assert info.has_audio and info.codec_audio == "aac"
    assert info.codec_video == "h264"
    assert info.bitrate > 100_000          # um testsrc2 720p nunca fica abaixo disso
    assert info.resolution_display == "1280x720"


def test_probe_e_cacheado_pelo_par_tamanho_mtime(sample_video):
    a = media.probe(sample_video)
    b = media.probe(sample_video)
    assert a is b, "segundo probe deveria vir do cache (mesmo objeto)"
    media.invalidate_cache(sample_video)
    c = media.probe(sample_video)
    assert c is not a, "invalidate_cache precisa descartar a entrada antiga"


def test_probe_or_raise_em_arquivo_inexistente(tmp_path):
    with pytest.raises(MediaError) as exc:
        media.probe_or_raise(tmp_path / "nao_existe.mp4")
    err = exc.value
    assert err.stage == "media"
    assert str(err) == err.message
    assert "nao_existe" in str(err)


def test_probe_de_arquivo_sem_video_levanta_media_error_com_detail(tmp_path):
    """Um .mp4 que não é vídeo: o stderr do ffprobe tem de aparecer no .detail."""
    fake = tmp_path / "lixeiro.mp4"
    fake.write_bytes(b"this is definitely not a video file\n" * 20)
    with pytest.raises(MediaError) as exc:
        media.probe_or_raise(fake)
    err = exc.value
    assert err.detail, "esperava o stderr do ffprobe em .detail"
    assert str(err) != err.detail          # mensagem humana x detalhe técnico
    assert isinstance(err, Exception)


def test_is_supported_aceita_video_e_recusa_o_resto():
    ok, why = media.is_supported(Path("clip.mp4"))
    assert ok and why == ""
    ok, why = media.is_supported(Path("musica.mp3"))
    assert not ok and "áudio" in why.lower()
    ok, why = media.is_supported(Path("nota.txt"))
    assert not ok and isinstance(why, str) and len(why) > 10, "deve explicar o motivo"


def test_frame_at_devolve_bgr_no_tamanho_pedido(sample_video):
    frame = media.frame_at(sample_video, 2.0, width=640)
    assert frame is not None
    h, w, ch = frame.shape
    assert w == 640 and ch == 3
    assert 350 <= h <= 370, f"proporção 16:9 esperada, veio {w}x{h}"
    assert frame.mean() > 20, "quadro escuro demais: a decodificação falhou"
    assert media.frame_at(sample_video.parent / "nao_existe.mp4", 1.0) is None


def test_extract_wav_monocanal_16khz(sample_video):
    wav = media.extract_wav(sample_video)
    assert wav.is_file() and wav.stat().st_size > 100_000
    st = next(s for s in ffprobe_json(wav)["streams"] if s["codec_type"] == "audio")
    assert st["codec_name"] == "pcm_s16le"
    assert int(st["sample_rate"]) == 16000
    assert int(st["channels"]) == 1
    # 16 kHz s16 mono ~= 32 KB/s
    assert abs(wav.stat().st_size / 32000 - 14.0) < 1.0


def test_curva_de_energia_mostra_o_mudo_entre_4_e_7s(sample_video):
    times, db = media.audio_energy_curve(sample_video)
    assert times.shape == db.shape and times.size > 100
    assert times[0] == pytest.approx(0.05, abs=0.05)
    silent = db[(times >= 4.5) & (times <= 6.5)]
    loud = db[(times <= 3.0) | (times >= 8.0)]
    assert silent.size > 10 and loud.size > 20
    assert float(silent.max()) <= -60.0, f"janela muda com energia {silent.max():.1f} dB"
    assert float(loud.min()) >= -40.0, f"janela alta com energia {loud.min():.1f} dB"
    assert float(loud.mean()) - float(silent.mean()) > 30.0


def test_estimativa_de_tamanho_cresce_com_a_duracao(sample_video):
    info = media.probe(sample_video)
    short = media.estimate_export_size(info, 10.0, 23, "1080p")
    long_ = media.estimate_export_size(info, 60.0, 23, "1080p")
    assert short > 0
    assert long_ > short * 4, "estimativa deveria escalar quase linearmente"
    crf35 = media.estimate_export_size(info, 10.0, 35, "1080p")
    assert crf35 < short, "CRF maior (menos qualidade) deve caber em menos bytes"


def test_container_suporta_stream_copy_para_h264_mp4(sample_video):
    assert media.container_supports_stream_copy(sample_video) is True
    assert media.container_supports_stream_copy(Path("clip.webm")) is False


def test_thumbnail_escreve_jpeg_valido(sample_video, tmp_path):
    dest = tmp_path / "thumb.jpg"
    out = media.thumbnail(sample_video, at=1.0, size=320, dest=dest)
    assert out == dest and dest.is_file() and dest.stat().st_size > 1000
    img = cv2.imread(str(dest))
    assert img is not None and 0 < img.shape[1] <= 320


def test_video_url_or_path_escapa_dois_pontos_e_apas():
    esc = media.video_url_or_path("/tmp/a:b'.mp4")
    assert ":" in esc and "'" not in esc.replace("\\'", ""), \
        f"esperava os dois-pontos escapados, veio {esc!r}"
