"""Fixtures compartilhadas da suíte de testes do CorteIA Studio.

Tudo aqui usa FFmpeg/ffprobe reais: nenhum teste desta suíte simula um arquivo
de vídeo — quando algo precisa de mídia, ela é gerada de fato.

Isolamento de filesystem
------------------------
``corteia/paths.py`` resolve os diretórios do app **no momento da chamada** a
partir de variáveis de ambiente (nada é congelado no import):

* Linux ``data_dir()``     -> ``$XDG_CONFIG_HOME/corteia-studio``
* Linux ``cache_dir()``    -> ``$XDG_CACHE_HOME/corteia-studio``
* ``temp_root()``          -> ``$TMPDIR/corteia-studio``

Logo a fixture ``isolated_ctx`` aponta as três para ``tmp_path`` antes de
construir o :class:`AppContext`, e nenhuma teste escreve em ``~/.config``,
``~/.cache`` ou ``/tmp/corteia-studio`` de verdade.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Qt sem display: definido antes do primeiro import do PySide6.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def have(binary: str) -> bool:
    from shutil import which
    return which(binary) is not None


def ffprobe_json(path: Path) -> dict:
    """``ffprobe -print_format json`` de um arquivo real."""
    import json
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def media_stream(path: Path, kind: str = "video") -> dict:
    return next((s for s in ffprobe_json(path)["streams"] if s["codec_type"] == kind), {})


def run_ffmpeg(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          check=check, capture_output=True, text=True)


def build_silent_sample(dest: Path, *, seconds: float = 14.0) -> Path:
    """14 s de testsrc2 1280x720@30 + sine 330 Hz mudo entre 4 s e 7 s.

    O recorte de silêncio é feito com ``volume`` expressivo, avaliador por
    quadro, exatamente como em ``scripts/_test_render.py``.
    """
    run_ffmpeg(
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
        "-f", "lavfi", "-i",
        f"sine=frequency=330:duration={seconds},"
        "amix=inputs=1,volume='if(between(t,4,7),0,1)':eval=frame",
        "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(dest))
    return dest


def build_hardcut_sample(dest: Path, *, colors=("red", "green", "blue"), each: float = 3.0) -> Path:
    """Clipe com N cortes secos (fundos sólidos), para o detector de cenas."""
    parts = []
    for i, c in enumerate(colors):
        p = dest.with_name(f"{dest.stem}_{i}.mp4")
        run_ffmpeg("-f", "lavfi", "-i",
                   f"color=c={c}:size=640x360:rate=30:duration={each}",
                   "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(p))
        parts.append(p)
    listing = dest.with_suffix(".txt")
    listing.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
    run_ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(dest))
    return dest


def make_transcript(texts, *, start: float = 0.4, dur_each: float = 3.6,
                    gap: float = 0.3, speakers=(0, 1)):
    """Transcrição sintética com timestamps por palavra (estrutura real)."""
    from corteia.models import Segment, Transcript, Word
    segs, t = [], float(start)
    for i, line in enumerate(texts):
        words = line.split()
        step = dur_each / max(1, len(words))
        ws, acc = [], t
        for w in words:
            ws.append(Word(round(acc, 3), round(acc + step, 3), w))
            acc += step
        segs.append(Segment(round(t, 3), round(acc, 3), line, ws, speakers[i % len(speakers)]))
        t = acc + gap
    return Transcript("pt", "base", segs)


# --------------------------------------------------------------------------- #
# midia real
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def ffmpeg_available() -> None:
    if not (have("ffmpeg") and have("ffprobe")):
        pytest.skip("ffmpeg/ffprobe não estão no PATH")


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory, ffmpeg_available) -> Path:
    """14 s, 1280x720@30, áudio senoidal com mudo de 4 a 7 s (sessão inteira)."""
    d = tmp_path_factory.mktemp("media")
    return build_silent_sample(d / "sample.mp4")


@pytest.fixture(scope="session")
def cuts_video(tmp_path_factory, ffmpeg_available) -> Path:
    """9 s com três cortes secos: a referência do detector de cenas."""
    d = tmp_path_factory.mktemp("cuts")
    return build_hardcut_sample(d / "cuts.mp4")


@pytest.fixture(scope="session")
def speech_video(ffmpeg_available) -> Path:
    """Fala real já transcrita pelos scripts de verificação do projeto."""
    p = Path("/tmp/asr_test.mp4")
    if not p.is_file():
        pytest.skip("/tmp/asr_test.mp4 não existe (rode scripts/_smoke_asr.py)")
    return p


@pytest.fixture(scope="session")
def faces_video() -> Path:
    """Clipe com rostos sintéticos gerado por scripts/_make_faces_video.py."""
    p = Path("/tmp/faces_test.mp4")
    if not p.is_file():
        pytest.skip("/tmp/faces_test.mp4 não existe (rode scripts/_make_faces_video.py)")
    return p


@pytest.fixture(scope="session")
def yunet_model() -> Path:
    from corteia import paths
    from corteia.core.faces import YUNET_FILE
    p = paths.bundled_model(YUNET_FILE)
    if not p.is_file():
        pytest.skip(f"modelo YuNet ausente: {p}")
    return p


# --------------------------------------------------------------------------- #
# transcrições de referência
# --------------------------------------------------------------------------- #
@pytest.fixture
def transcript():
    """Três falas alternando dois locutores, ~13 s no total."""
    return make_transcript([
        "Eu larguei um salário de doze mil reais pra trabalhar de casa e não me arrependo.",
        "A verdade é que estabilidade no emprego é uma ilusão e você é sempre o primeiro corte.",
        "Então anota a lição de hoje: vende aquilo que você já faz todo dia de graça.",
    ])


# --------------------------------------------------------------------------- #
# contexto isolado
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_env(tmp_path, monkeypatch) -> Path:
    """Redireciona config, cache, projetos e temp do app para ``tmp_path``.

    Os nomes reais das variáveis vêm de ``corteia/paths.py``:
    ``XDG_CONFIG_HOME`` (data_dir), ``XDG_CACHE_HOME`` (cache_dir) e
    ``TMPDIR`` (temp_root via ``tempfile.gettempdir``).
    """
    cfg_root = tmp_path / "xdg-config"
    cache_root = tmp_path / "xdg-cache"
    tmp_root = tmp_path / "tmp"
    for d in (cfg_root, cache_root, tmp_root):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_root))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    monkeypatch.setenv("TMPDIR", str(tmp_root))

    # os singletons de módulo precisam ser descartados junto com o ambiente
    import corteia.config as config_mod
    import corteia.core.llm as llm_mod
    monkeypatch.setattr(config_mod, "_config", None, raising=False)
    monkeypatch.setattr(llm_mod, "_client", None, raising=False)
    return tmp_path


@pytest.fixture
def isolated_ctx(isolated_env):
    """:class:`AppContext` inteiro morando em ``tmp_path`` — nada toca ``~/``."""
    from corteia.config import Config
    from corteia.context import AppContext
    from corteia.project import ProjectStore

    config = Config(Path(isolated_env) / "xdg-config" / "corteia-studio" / "config.json")
    ctx = AppContext(config)
    assert str(ctx.store.dir).startswith(str(isolated_env)), \
        "isolamento falhou: projetos fora de tmp_path"
    yield ctx
    ProjectStore.__init__  # apenas para deixar claro que o store é por contexto


@pytest.fixture
def tmp_config(isolated_env) -> "object":
    from corteia.config import Config
    return Config(Path(isolated_env) / "xdg-config" / "corteia-studio" / "config.json")
