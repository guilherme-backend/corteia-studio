# -*- mode: python ; coding: utf-8 -*-
"""Spec do PyInstaller — CorteIA Studio (one-dir).

    pyinstaller corteia.spec                    # na máquina ALVO
    build_windows.bat  /  build_linux.sh        # scripts que chamam isto

one-dir é deliberado: faster-whisper, CTranslate2, OpenCV e PySide6 carregam
.DLL/.so em tempo de execução, e o modo one-file extrai tudo para o TEMP a cada
abertura (inicialização lenta + antivírus desconfiado).

AUDITORIA (feita contra o código em corteia/, revisão de 2026-11-14)
--------------------------------------------------------------------
O que é importado DENTRO de função e o analizador estático não vê:

  faster_whisper          core/transcribe.py:540
  ctranslate2             hardware.py:201  (via `_cuda_runtime_ok`)
  onnxruntime             faster_whisper/vad.py:298  (fora do nosso repo)
  av                      faster_whisper/audio.py    (por cima de fw preguiçoso)
  huggingface_hub         core/transcribe.py:416/450
  scenedetect.*           core/scenes.py:28/44/45/78/79
  yt_dlp(.utils)          core/downloader.py:162/169
  psutil / tqdm           core/transcribe.py:227/236/398/586
  cv2 / numpy             core/analyze.py:240 e ui/pages/settings_page.py:910
  PySide6.QtMultimedia(+Widgets)  ui/widgets/player.py:36/295/346
  shiboken6               ui/widgets/cards.py:192
  winreg                  core/fonts.py:85  (só Windows)

Correções aplicadas nesta revisão:

  * `PIL` / `PIL.Image` SAÍRAM de `hiddenimports`. Nada em `corteia/` importa
    Pillow (verificado por grep), nenhum dos nossos deps importa (`Required-by:`
    vazio em `pip show pillow`), e `requirements.txt` diz explicitamente que
    Pillow é só do pipeline de ícones em `assets/make_icons.py`. Manter o
    hiddenimport embutia ~7 MB de código morto no instalador.
  * `pywintypes` SAIU de `hiddenimports`: nenhum módulo `win32*` é importado por
    `corteia/` (a doc de `requirements.txt` fala em AppUserModelID, mas o código
    não existe). `winreg` fica: é stdlib do Windows e é usado de verdade.
  * `collect_submodules("corteia")` entra na lista. A lista manual anterior
    esquecia módulos reais: `corteia.core.errors`, `.ffmpeg_cli`, `.lexicon`,
    `.reframe`, `.silence`, `.timeline`, `corteia.config`, `corteia.context`,
    `corteia.main`, `corteia.models`, `corteia.paths`. A lista manual continua
    como documentação e cinto-de-segurança; o conjunto é deduplicado.
  * `binaries`: `ctranslate2` NÃO tem hook nem no PyInstaller nem no
    pyinstaller-hooks-contrib (verificado na árvore do repo dos hooks: a faixa
    `hook-cairocffi` → `hook-cv2` não tem `hook-ctranslate2.py`). Sem coleta
    explícita, as DLLs que o `_ext.pyd` abre em tempo de execução ficam de fora.
  * `datas`: faltava `assets/`. `ui/app.py:76`, `ui/windows/main_window.py:114`
    e `ui/widgets/sidebar.py:212` procuram `assets/icon.png` relativo ao bundle
    (`paths.bundle_root()`), ou seja `dist/CorteIA Studio/assets/icon.png`. Sem
    isso o ícone da janela caía no desenho vetorial do tema.
  * `corteia/resources/icons/` AGORA existe (só um LEIA-ME.txt). O docstring
    antigo dizia que não existia; o guard continua, porque a pasta pode sumir de
    novo e listar caminho inexistente em `datas` derruba o build.

Nada aqui foi executado: o ambiente de referência é Linux, sem Windows e sem
`iscc`. `build_windows.bat` chama `scripts/check_package.py` depois do freeze
para conferir o bundle real.
"""

import os
import sys
from pathlib import Path

try:
    from PyInstaller.utils.hooks import (
        collect_data_files,
        collect_dynamic_libs,
        collect_submodules,
        get_package_paths,
    )
except ImportError:                                    # pragma: no cover
    collect_data_files = collect_dynamic_libs = get_package_paths = None

    def collect_submodules(_m):                        # type: ignore
        return []


APP_NAME = "CorteIA Studio"
ROOT = Path(SPECPATH).resolve()                        # noqa: F821
IS_WIN = sys.platform == "win32"
_TAG = "[corteia.spec]"


def log(msg):
    print(f"{_TAG} {msg}", flush=True)


def section(title):
    print(f"\n{_TAG} " + "-" * 70, flush=True)
    print(f"{_TAG} {title}", flush=True)
    print(f"{_TAG} " + "-" * 70, flush=True)


# --------------------------------------------------------------------------- #
# 0. log verboso de ambiente + aviso de plataforma errada
# --------------------------------------------------------------------------- #
section("0. ambiente do build")
try:
    import PyInstaller

    _pi_ver = PyInstaller.__version__
except Exception:                                       # pragma: no cover
    _pi_ver = "?"

log(f"PyInstaller ....... {_pi_ver}")
log(f"python ............ {sys.version.split()[0]}  ({sys.executable})")
log(f"implementation .... {sys.implementation.name}")
log(f"sys.platform ...... {sys.platform}   (os.name={os.name})")
log(f"arch .............. {os.environ.get('PROCESSOR_ARCHITECTURE', '')}"
    f"{os.uname().machine if hasattr(os, 'uname') else ''}")
log(f"SPECPATH / ROOT ... {ROOT}")
log(f"console=False, upx=False (UPX costuma quebrar as DLLs do Qt e assina "
    f"mal o bundle)")

if not IS_WIN:
    # O aviso é alto porque o erro é silencioso: um build Linux "passa" e gera
    # um ELF, não um .exe. O instalador Windows precisa rodar NO Windows.
    log("=" * 70)
    log("AVISO: sys.platform != win32 -> este NÃO é um build Windows.")
    log(
        "       O PyInstaller NÃO faz cross-compile: o que sair daqui é um "
        f"binário para {sys.platform!r}, sem Qt6*.dll, sem qwindows.dll e sem "
        "qualquer DLL de ctranslate2/av/cv2 do Windows."
    )
    log("       O freeze do instalador Windows tem que rodar numa máquina "
        "Windows 10/11 x64 real:")
    log("           build_windows.bat              (local, ou)")
    log("           .github/workflows/build-windows.yml   (CI, windows-latest)")
    log("       Rodar aqui só serve para checar sintaxe do spec e os caminhos "
        "de `datas`.")
    log("=" * 70)

_missing_pkgs = []
for _pkg in ("faster_whisper", "ctranslate2", "av", "onnxruntime", "cv2",
             "scenedetect", "yt_dlp", "PySide6", "numpy", "psutil"):
    try:
        __import__(_pkg)
        log(f"importável ...... {_pkg}")
    except Exception as _exc:                           # noqa: BLE001
        _missing_pkgs.append(_pkg)
        log(f"FALTA no env .... {_pkg}  ({type(_exc).__name__}: {_exc})")
if _missing_pkgs:
    log(f"AVISO: {len(_missing_pkgs)} pacote(s) ausente(s) no ambiente de build "
        f"-> {_missing_pkgs}. Instale com `pip install -r requirements.txt`.")


# --------------------------------------------------------------------------- #
# 1. recursos do pacote (corteia/resources/*) + assets/
# --------------------------------------------------------------------------- #
section("1. datas — recursos que vão para dentro do bundle")
_resource_dirs = ["qss", "models", "i18n", "icons"]
datas = []
for sub in _resource_dirs:
    src = ROOT / "corteia" / "resources" / sub
    if src.is_dir():
        dest = f"corteia/resources/{sub}"
        datas.append((str(src), dest))
        files = sorted(p.name for p in src.iterdir() if p.is_file())
        log(f"datas  + corteia/resources/{sub:<8} -> {dest}  "
            f"({len(files)} arquivo(s): {', '.join(files) or 'vazio'})")
    else:
        log(f"datas  PULADO (não existe no disco): corteia/resources/{sub}")

# `assets/icon.png` é lido em runtime por ui/app.py, ui/windows/main_window.py
# e ui/widgets/sidebar.py via paths.bundle_root()/assets. No one-dir isso é
# dist/CorteIA Studio/assets/icon.png. O .ico vai junto para o atalho poder
# apontar para dentro do bundle se necessário (o .iss também copia o seu).
_assets = ROOT / "assets"
if _assets.is_dir():
    datas.append((str(_assets), "assets"))
    for name in ("icon.png", "icon.ico", "icon.svg"):
        mark = "ok " if (_assets / name).is_file() else "FALTA"
        log(f"datas  assets/{name:<9} [{mark}]")
else:
    log("datas  PULADO: assets/ não existe (ícone da janela vira o vetorial do tema)")

# Nada de modelos Whisper em `datas`: eles vão para data_dir()/models na
# primeira execução (paths.models_dir) e pesam de 78 MB a 3,1 GB.

if collect_data_files:
    # qss/scss do próprio Qt e plugins que o hook do PySide6 não pega em builds
    # enxutos. Falha silenciosa de propósito: só existe `**/*.qss` em algumas
    # versões do wheel.
    try:
        _qtqss = collect_data_files("PySide6", includes=["**/*.qss"])
        datas += _qtqss
        log(f"datas  + {len(_qtqss)} .qss do próprio PySide6")
    except Exception as _exc:                           # noqa: BLE001
        log(f"datas  collect_data_files('PySide6', qss) falhou: {_exc}")


# --------------------------------------------------------------------------- #
# 2. binaries — DLLs abertas em tempo de execução
# --------------------------------------------------------------------------- #
section("2. binaries — DLLs/.so coletadas explicitamente")
binaries = []


def _collect_libs(module, reason):
    """collect_dynamic_libs protegido: some o pacote, o build não cai."""
    if not collect_dynamic_libs:
        log(f"binaries PULADO ({module}): hooks do PyInstaller indisponíveis")
        return []
    try:
        found = collect_dynamic_libs(module)
    except Exception as _exc:                           # noqa: BLE001
        log(f"binaries ERRO em collect_dynamic_libs({module!r}): {_exc}")
        return []
    log(f"binaries + {len(found):>3} lib(s) de {module:<14} # {reason}")
    for src, dest in sorted(found)[:12]:
        log(f"          {os.path.basename(str(src)):<44} -> {dest}")
    if len(found) > 12:
        log(f"          ... e mais {len(found) - 12}")
    return found


# ctranslate2: NÃO existe hook upstream (nem PyInstaller, nem hooks-contrib).
# É a DLL que `import ctranslate2` abre; sem isto o ASR quebra em runtime com
# "DLL load failed" numa máquina limpa. Também cobre as librerias que o wheel
# embute (oneDNN/MKL/OpenMP, quando houver).
binaries += _collect_libs("ctranslate2", "sem hook upstream — obrigatório aqui")

# onnxruntime (provider plugins em onnxruntime/capi) e cv2 (cv2.pyd +
# opencv_videoio_ffmpeg*.dll): têm hook próprio no pyinstaller-hooks-contrib
# (hook-onnxruntime.py, hook-cv2.py), que roda pela Análise. A chamada abaixo é
# redundância deliberada e barata — PyInstaller deduplica destino igual, e
# `strict_collect_mode` está desligado — para o bundle não depender de qual
# versão de hook está no venv da máquina de build.
binaries += _collect_libs("onnxruntime", "VAD Silero; hook existe, redundância ok")
binaries += _collect_libs("cv2", "cv2.pyd + opencv_videoio_ffmpeg*.dll")

# av / PyAV: no Windows moderno do PyPI as DLLs ficam em site-packages/av.libs
# (não dentro de av/), e o hook-av.py as coleta como DATA preservando a pasta
# `av.libs/` — de propósito, para a análise binária não criar uma cópia no
# topo. Por isso NÃO chamamos collect_dynamic_libs("av") sempre; só como
# fallback se nem `av/` nem `av.libs/` existirem no env de build.
if get_package_paths:
    try:
        _av_base, _av_dir = get_package_paths("av")
        _av_libs = Path(_av_base) / "av.libs"
        if _av_libs.is_dir():
            _n = len(list(_av_libs.glob("*.dll"))) + len(list(_av_libs.glob("*.so*")))
            log(f"binaries av.libs/ já existe ({_n} lib(s)) -> hook-av.py cuida; "
                f"não duplicamos")
            binaries.append((str(_av_libs), "av.libs"))
        elif any(Path(_av_dir).glob("*avcodec*")) or any(Path(_av_dir).glob("*.dll")):
            binaries += _collect_libs("av", "DLLs dentro do próprio pacote av/")
        else:
            log("binaries AV: nem av.libs/ nem DLLs em av/ no env de build — "
                "o freeze vai quebrar em runtime. Reinstale `av`.")
    except Exception as _exc:                           # noqa: BLE001
        log(f"binaries AV: inspeção falhou ({_exc})")
else:
    binaries += _collect_libs("av", "fallback sem get_package_paths")

# PySide6 / Qt: o hook oficial do PyInstaller já põe Qt6Core/Gui/Widgets/... e
# os plugins dos módulos importados (destino `PySide6/Qt/plugins/<tipo>/`). Os
# tipos abaixo são reafirmados explicitamente porque o app depende de
# `platforms/qwindows.dll` para ABRIR JANELA e porque o hook só coleta o plugin
# `multimedia` se `PySide6.QtMultimedia` estiver de fato na lista de módulos —
# que é exatamente o caso aqui (hiddenimports abaixo + player.py o importa
# dentro de try/except).
_QT_PLUGIN_TYPES = ["platforms", "styles", "imageformats", "multimedia",
                    "iconengines", "platforminputcontexts", "networkinformation",
                    "tls"]
_qt_dirs = []
try:
    _ps_base, _ps_dir = (get_package_paths("PySide6") if get_package_paths
                         else (None, None))
    if _ps_dir:
        _qt_dirs = [Path(_ps_dir) / "Qt" / "plugins", Path(_ps_dir) / "plugins"]
except Exception as _exc:                               # noqa: BLE001
    log(f"binaries PySide6: get_package_paths falhou ({_exc})")

_qt_plugin_root = next((d for d in _qt_dirs if d.is_dir()), None)
if _qt_plugin_root:
    log(f"binaries Qt plugins raiz: {_qt_plugin_root}")
    for ptype in _QT_PLUGIN_TYPES:
        pdir = _qt_plugin_root / ptype
        if not pdir.is_dir():
            log(f"binaries   (sem pasta de plugin: {ptype})")
            continue
        libs = [p for p in sorted(pdir.iterdir())
                if p.is_file() and p.suffix.lower() in (".dll", ".so")]
        # destino idêntico ao do hook para o PyInstaller colapsar os dois
        dest = str(Path("PySide6") / "Qt" / "plugins" / ptype)
        for lib in libs:
            binaries.append((str(lib), dest))
        log(f"binaries + {len(libs):>3} plugin(s) {ptype:<21} -> {dest}  "
            f"[{', '.join(l.name for l in libs) or 'vazio'}]")
    # DLLs do Qt em si (Windows: PySide6/Qt6*.dll). Verificação de sanidade: se a
    # pasta não tiver NENHUMA Qt6*.dll no Windows, o bundle nasce quebrado.
    qt_root = Path(_qt_plugin_root).parent.parent if _qt_plugin_root else None
    if qt_root and IS_WIN:
        qt_dlls = sorted(p.name for p in qt_root.glob("Qt6*.dll"))
        need = ["Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll",
                "Qt6Multimedia.dll", "Qt6Svg.dll", "Qt6Network.dll"]
        log(f"binaries Qt6*.dll em {qt_root}: {len(qt_dlls)}")
        for n in need:
            log(f"          {n:<22} [{'ok' if n in qt_dlls else 'FALTA'}]"
                f"  (entra como dependência do módulo PySide6.Qt* importado)")
        for n in need:
            p = qt_root / n
            if p.is_file():
                binaries.append((str(p), "PySide6"))
else:
    log("binaries PySide6: pasta de plugins não localizada; o hook oficial "
        "ainda assim roda, mas confira o bundle com scripts/check_package.py")

# FFmpeg: o app usa o binário do sistema/PATH (requirements.txt explica) e pode
# achar ffmpeg.exe/ffprobe.exe dentro de `{app}` ou `{app}/bin`. Para embutir,
# copie os dois para third_party/ffmpeg/ antes do freeze — as linhas abaixo já
# fazem isso se os arquivos estiverem lá.
_tp = ROOT / "third_party" / "ffmpeg"
if _tp.is_dir():
    exes = [p for p in sorted(_tp.iterdir())
            if p.is_file() and p.stem in ("ffmpeg", "ffprobe")]
    for p in exes:
        binaries.append((str(p), "bin"))
    if exes:
        log(f"binaries + {len(exes)} binário(s) FFmpeg de third_party/ffmpeg -> bin/")
    else:
        log("binaries third_party/ffmpeg: vazio -> FFmpeg vem do PATH do destino")

# --------------------------------------------------------------------------- #
# 3. hiddenimports — o que é importado DENTRO de função e o analizador não vê
# --------------------------------------------------------------------------- #
section("3. hiddenimports")
_hidden = [
    # ASR: corteia/core/transcribe.py:540 e ui/pages/settings_page.py:916
    "faster_whisper",
    # backend de inferência que faster-whisper abre por dentro; carrega DLLs
    "ctranslate2",
    # VAD Silero / runtime ONNX: faster_whisper/vad.py:298, requirements.txt:42
    "onnxruntime",
    # download de modelos: huggingface_hub importado em transcribe.py:416/450
    "huggingface_hub",
    "tokenizers",
    # decodificação de áudio em memória (faster_whisper/audio.py)
    "av",
    # vídeo/imagens. Pillow NÃO está aqui de propósito: ver docstring.
    "cv2",
    "numpy",
    # cenas: scenedetect importado em scenes.py:28/44/45/78/79
    "scenedetect",
    "scenedetect.detectors",
    "scenedetect.stats_manager",
    "scenedetect.video_stream",
    "scenedetect.scene_manager",
    "scenedetect.backends",
    "scenedetect.backends.opencv",
    # download por URL: downloader.py:162/169
    "yt_dlp",
    "yt_dlp.utils",
    # util: psutil em hardware.py:115 e transcribe.py:227/236/586; tqdm em 398
    "psutil",
    "tqdm",
    # Qt: player.py:36/295/346 importa QtMultimedia/QtMultimediaWidgets dentro
    # de blocos de fallback, e o analizador costuma perder isso.
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtCore",
    # QtSvg não é importado por corteia/, mas é o que traz Qt6Svg.dll +
    # imageformats/qsvg.dll; sem eles QIcon não desenha SVG. Barato e útil.
    "PySide6.QtSvg",
    "shiboken6",                       # ui/widgets/cards.py:192
]

if IS_WIN:
    # core/fonts.py:85 usa winreg para enumerar as fontes do sistema.
    _hidden += ["winreg"]

# Módulos do próprio app. A lista explícita documenta o que é carregado tarde
# (pipeline.py:27/325 abre core.faces e core.transcribe dentro de função; as
# páginas são montadas por nome em ui/pages/__init__.py); o collect_submodules
# garante que um módulo NOVO em corteia/ entre no bundle sem editar o spec.
_hidden += [
    "corteia.core.faces",              # pipeline.py:325, import dentro de função
    "corteia.core.transcribe",         # pipeline.py:27, e carregado tarde na UI
    "corteia.core.render",
    "corteia.core.subtitles",
    "corteia.core.llm",
    "corteia.core.fonts",
    "corteia.core.downloader",
    "corteia.core.media",
    "corteia.core.analyze",
    "corteia.core.exporter",
    "corteia.core.pipeline",
    "corteia.core.errors",
    "corteia.core.ffmpeg_cli",
    "corteia.core.lexicon",
    "corteia.core.reframe",
    "corteia.core.silence",
    "corteia.core.timeline",
    "corteia.workers.jobs",
    "corteia.main",
    "corteia.ui.app",
    "corteia.ui.theme",
    "corteia.ui.windows.main_window",
    "corteia.ui.windows.preview_dialog",
    "corteia.ui.pages.dashboard",
    "corteia.ui.pages.import_page",
    "corteia.ui.pages.clips_page",
    "corteia.ui.pages.editor_page",
    "corteia.ui.pages.export_page",
    "corteia.ui.pages.settings_page",
    "corteia.i18n",
    "corteia.project",
    "corteia.config",
    "corteia.context",
    "corteia.hardware",
    "corteia.models",
    "corteia.paths",
]

for _mod in ("corteia", "corteia.ui.widgets", "yt_dlp.extractor"):
    try:
        _subs = collect_submodules(_mod)
        _hidden += _subs
        log(f"hidden + {len(_subs):>4} submódulos de {_mod}")
    except Exception as _exc:                           # noqa: BLE001
        log(f"hidden ERRO em collect_submodules({_mod!r}): {_exc}")

# dedup preservando ordem
seen = set()
hiddenimports = []
for name in _hidden:
    if name not in seen:
        seen.add(name)
        hiddenimports.append(name)
log(f"hiddenimports ..... {len(hiddenimports)} entradas únicas")
for name in hiddenimports[:0]:                          # (silencioso por padrão)
    log(f"                    {name}")


# --------------------------------------------------------------------------- #
# 4. exe
# --------------------------------------------------------------------------- #
icon_file = ROOT / "assets" / "icon.ico"
log(f"icon .............. {icon_file} [{'ok' if icon_file.is_file() else 'FALTA -> sem ícone no .exe'}]")

_EXCLUDES = [
    "tkinter",
    "pydoc_data.topics",
    # torch não é usado de propósito (requirements.txt explica); hardware.py:209
    # tenta importá-lo para detectar CUDA e trata ImportError. Excluir economiza
    # ~2,5 GB de bundle. openai/PIL extras também: llm.py fala HTTP por urllib.
    "torch",
    "torchaudio",
    "numpy.tests",
    "numpy.f2py.tests",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQml",
    "PySide6.QtPdf",
    "PySide6.QtBluetooth",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtTest",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "setuptools",
    "pkg_resources",
    "pytest",
    "IPython",
    "matplotlib",
    "test",
    "unittest.tests",
]
# `numpy` e `av` ficam FORa dos excludes por definição: o ASR e o scenedetect
# dependem dos dois. Excluir um deles congela um bundle que abre e morre na
# primeira análise.
_BAD_EXCLUDES = {"numpy", "av", "cv2", "ctranslate2", "PySide6", "PySide6.QtCore"}
assert not (_BAD_EXCLUDES & set(_EXCLUDES)), "exclude perigoso no spec"
log(f"excludes .......... {len(_EXCLUDES)} (seguros: numpy e av intocados)")

section("4. Analysis / EXE / COLLECT")

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "pyi_rthook_corteia.py")]
    if (ROOT / "pyi_rthook_corteia.py").is_file() else [],
    excludes=_EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# --------------------------------------------------------------------------- #
# 5. resumo do que ENTROU no bundle (log verboso pós-análise)
# --------------------------------------------------------------------------- #
section("5. RESUMO DO PACK (pós-Analysis)")
log(f"módulos python .... {len(a.pure)}")


def _dump_group(label, toc, exts=None, limit=200):
    rows = []
    for item in toc:
        # PyInstaller 6 usa TOC de (dest, src, typecode); aceitar tuplas antigas
        try:
            dest, src = item[0], item[1]
        except Exception:                               # noqa: BLE001
            continue
        name = str(dest)
        if exts and not name.lower().endswith(exts):
            continue
        rows.append((name, str(src)))
    rows.sort()
    log(f"{label}: {len(rows)} entrada(s)")
    for name, src in rows[:limit]:
        log(f"    {name:<58} <- {os.path.basename(src)}")
    if len(rows) > limit:
        log(f"    ... e mais {len(rows) - limit}")


_dump_group("binaries (DLL/.so/.pyd)", list(a.binaries),
            exts=(".dll", ".so", ".pyd", ".dylib"))
_dump_group("datas (recursos)", list(a.datas))

# checagens de sanidade que dão medo bom em build de release
_expect = ["qwindows.dll" if IS_WIN else "libqxcb.so",
           "ctranslate2", "av", "onnxruntime", "cv2",
           "corteia/resources/qss", "corteia/resources/i18n",
           "corteia/resources/models", "assets/"]
_all_dests = " | ".join(str(i[0]) for i in list(a.binaries) + list(a.datas))
for token in _expect:
    mark = "PRESENTE" if token in _all_dests else "AUSENTE"
    log(f"sanidade {token:<26} {mark}")
log("sanidade: as extensões .pyd/.dll acima só são resolvidas no Windows; em "
    "Linux esta lista é indicativa.")
log("conferência definitiva do bundle: python scripts/check_package.py "
    "'dist/CorteIA Studio'")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    # Um nome só para exe e pasta: "CorteIA Studio" (o .iss e os scripts de
    # build assumem esse caminho; espaços exigem aspas no shell, ver
    # build_linux.sh).
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX quebra as DLLs do Qt e irrita antivírus
    console=False,                   # app gráfico: sem janela preta
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_file) if icon_file.is_file() else None,
)

# --------------------------------------------------------------------------- #
# 6. one-dir: a pasta "dist/CorteIA Studio/" com o executável "CorteIA Studio"
# --------------------------------------------------------------------------- #
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

section("fim do spec")
log(f"saída esperada: dist/{APP_NAME}/  ({'o .exe' if IS_WIN else 'o ELF'})")
if not IS_WIN:
    log("lembrete: NÃO é um instalador Windows. Rode build_windows.bat ou o "
        "workflow build-windows.yml numa máquina Windows x64.")
