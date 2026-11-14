#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_package.py — verificação automática do bundle PyInstaller.

Roda DEPOIS do freeze, sobre o diretório gerado (`dist/CorteIA Studio` no
Windows), e responde uma única pergunta: **o bundle está completo o bastante
para abrir numa máquina Windows limpa?**

    python scripts/check_package.py "dist/CorteIA Studio"
    python scripts/check_package.py "dist/CorteIA Studio" --verbose
    python scripts/check_package.py --dry-run          # só lista o que procura
    python scripts/check_package.py --dist "dist/CorteIA Studio" --json

Código de saída:
    0  todos os itens OBRIGATÓRIOS presentes
    1  pelo menos um item OBRIGATÓRIO faltando
    2  uso errado / diretório do bundle inexistente

Por que isto existe: com `console=False` o bundle quebra **mudo** — se faltar
`qwindows.dll` o processo morre antes de qualquer janela, sem mensagem. Este
script transforma "abra e veja" em tabela PRESENTE/FALTANDO no log do build.

Nada aqui depende de Windows: é só `pathlib` + `glob`, e os padrões aceitam as
duas formas que o PyInstaller produz (`PySide6/Qt/plugins/...` do hook oficial
e `PySide6/qtplugins/...` se algum dia o layout mudar). A tabela completa só
tem significado no bundle congelado NO Windows: em Linux os itens `.dll`/`.pyd`
aparecem como FALTANDO por definição.

Este script nunca abre o executável e não prova que o app funciona — prova só
que as peças estão na pasta.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "CorteIA Studio"
DEFAULT_DIST = os.path.join("dist", APP_NAME)

REQUIRED = "OBRIGATORIO"
OPTIONAL = "OPCIONAL"

# Nível -> (rótulo curto, pesa no código de saída?)
LEVELS = {REQUIRED: "OBR", OPTIONAL: "opt"}


@dataclass
class Item:
    """Uma checagem da tabela."""

    item_id: str
    label: str
    level: str
    patterns: list[str]                     # glob relativo à raiz do bundle
    hint: str = ""                          # o que fazer se faltar
    # famílias que precisam aparecer todas (para av: avcodec/avformat/...).
    # cada entrada é (nome_amigavel, substring_aceita_no_nome_do_arquivo)
    all_of: list[tuple[str, str]] = field(default_factory=list)
    # exigir tamanho mínimo (bytes) para não contar arquivo zerado
    min_bytes: int = 1

    def is_win_item(self) -> bool:
        return any(p.endswith(".dll") or ".pyd" in p or ".exe" in p
                   for p in self.patterns)


# --------------------------------------------------------------------------- #
#  A lista. Ordem = ordem da tabela.
# --------------------------------------------------------------------------- #
ITEMS: list[Item] = [
    Item(
        item_id="exe",
        label=f"executável {APP_NAME}.exe",
        level=REQUIRED,
        patterns=[f"{APP_NAME}.exe", APP_NAME],
        hint="rode `pyinstaller corteia.spec` e confira `name=APP_NAME` no EXE()",
        min_bytes=200_000,
    ),
    # ---- Qt / PySide6 ------------------------------------------------------ #
    Item(
        item_id="qt.qwindows",
        label="plugin de plataforma Qt (qwindows.dll)",
        level=REQUIRED,
        patterns=["PySide6/Qt/plugins/platforms/qwindows.dll",
                  "PySide6/qtplugins/platforms/qwindows.dll",
                  "*/plugins/platforms/qwindows.dll",
                  "**/platforms/qwindows.dll"],
        hint="sem isto o app MORRE sem janela ('could not find or load the Qt "
             "platform plugin windows'). Confira `PySide6.QtWidgets` em "
             "hiddenimports e a pasta PySide6/Qt/plugins/platforms do wheel.",
    ),
    Item(
        item_id="qt.platforms_any",
        label="algum plugin em platforms/",
        level=REQUIRED,
        patterns=["PySide6/Qt/plugins/platforms/*.dll",
                  "PySide6/qtplugins/platforms/*.dll",
                  "**/platforms/*.dll"],
    ),
    Item(
        item_id="qt.core",
        label="Qt6Core.dll",
        level=REQUIRED,
        patterns=["PySide6/Qt6Core.dll", "**/Qt6Core.dll"],
    ),
    Item(
        item_id="qt.gui",
        label="Qt6Gui.dll",
        level=REQUIRED,
        patterns=["PySide6/Qt6Gui.dll", "**/Qt6Gui.dll"],
    ),
    Item(
        item_id="qt.widgets",
        label="Qt6Widgets.dll",
        level=REQUIRED,
        patterns=["PySide6/Qt6Widgets.dll", "**/Qt6Widgets.dll"],
    ),
    Item(
        item_id="qt.multimedia",
        label="Qt6Multimedia.dll",
        level=REQUIRED,
        patterns=["PySide6/Qt6Multimedia.dll", "**/Qt6Multimedia.dll"],
        hint="o player (ui/widgets/player.py) abre QtMultimedia dentro de "
             "try/except; sem a DLL a janela de vídeo some sem erro visível.",
    ),
    Item(
        item_id="qt.svg",
        label="Qt6Svg.dll",
        level=OPTIONAL,
        patterns=["PySide6/Qt6Svg.dll", "**/Qt6Svg.dll"],
    ),
    Item(
        item_id="qt.plugins.styles",
        label="plugins styles/",
        level=OPTIONAL,
        patterns=["PySide6/Qt/plugins/styles/*.dll", "**/plugins/styles/*.dll"],
    ),
    Item(
        item_id="qt.plugins.imageformats",
        label="plugins imageformats/",
        level=OPTIONAL,
        patterns=["PySide6/Qt/plugins/imageformats/*.dll",
                  "**/plugins/imageformats/*.dll"],
    ),
    Item(
        item_id="qt.plugins.multimedia",
        label="plugins multimedia/ (backend de vídeo do Qt)",
        level=REQUIRED,
        patterns=["PySide6/Qt/plugins/multimedia/*.dll",
                  "**/plugins/multimedia/*.dll"],
        hint="é o plugin que faz QtMultimedia decodificar; precisa de "
             "`PySide6.QtMultimedia` em hiddenimports.",
    ),
    # ---- ASR / inferência -------------------------------------------------- #
    Item(
        item_id="ct2.dll",
        label="DLL do CTranslate2 (ctranslate2.dll)",
        level=REQUIRED,
        patterns=["ctranslate2/ctranslate2.dll", "**/ctranslate2*.dll"],
        hint="NÃO existe hook do PyInstaller para ctranslate2; o spec chama "
             "collect_dynamic_libs('ctranslate2'). Se faltar, o ASR quebra.",
    ),
    Item(
        item_id="ct2.ext",
        label="extensão python do CTranslate2 (_ext*.pyd)",
        level=REQUIRED,
        patterns=["ctranslate2/_ext*.pyd", "ctranslate2/*.pyd"],
    ),
    Item(
        item_id="ct2.libs",
        label="DLLs de apoio do CTranslate2 (oneDNN/MKL/OpenMP)",
        level=OPTIONAL,
        patterns=["ctranslate2/*.dll"],
        hint="informativo: quantas DLLs o collect_dynamic_libs trouxe.",
    ),
    Item(
        item_id="onnxruntime",
        label="onnxruntime (capi/*.pyd + providers)",
        level=REQUIRED,
        patterns=["onnxruntime/capi/onnxruntime_providers_shared.dll",
                  "onnxruntime/capi/*.pyd", "onnxruntime/capi/*.dll",
                  "**/onnxruntime*.pyd"],
        hint="usado pelo VAD do faster-whisper (faster_whisper/vad.py:298, "
             "import dentro de função).",
    ),
    # ---- PyAV (decodificação de áudio em memória) -------------------------- #
    Item(
        item_id="av.libs",
        label="DLLs do PyAV (av.libs/ ou av/)",
        level=REQUIRED,
        patterns=["av.libs/*.dll", "av/*.dll", "**/avcodec*.dll"],
        hint="no wheel Windows do PyAV as DLLs ficam em site-packages/av.libs; "
             "o hook-av.py as copia como data, preservando a pasta.",
    ),
    Item(
        item_id="av.families",
        label="famílias FFmpeg do PyAV (avcodec/avformat/avutil/"
              "swresample/swscale)",
        level=REQUIRED,
        patterns=["av.libs/*.dll", "av/*.dll", "PySide6/**/*.dll", "**/*.dll"],
        all_of=[("avcodec", "avcodec"), ("avformat", "avformat"),
                ("avutil", "avutil"), ("swresample", "swresample"),
                ("swscale", "swscale")],
        hint="o PyAV precisa das cinco; o backend multimídia do Qt também traz "
             "avcodec/avformat, então confira o caminho onde cada uma foi "
             "achada antes de assumir que o PyAV está coberto.",
    ),
    # ---- OpenCV ------------------------------------------------------------ #
    Item(
        item_id="cv2.pyd",
        label="extensão cv2 (cv2*.pyd)",
        level=REQUIRED,
        patterns=["cv2/cv2*.pyd", "cv2/*.pyd", "**/cv2*.pyd"],
        hint="scenedetect e core/faces.py dependem de cv2; o hook-cv2.py "
             "resolve o layout do wheel.",
    ),
    Item(
        item_id="cv2.ffmpeg",
        label="cv2 opencv_videoio_ffmpeg*.dll",
        level=OPTIONAL,
        patterns=["cv2/opencv_videoio_ffmpeg*.dll", "**/opencv_videoio_ffmpeg*.dll"],
        hint="sem ela cv2.VideoCapture não abre alguns codecs (o app usa "
             "FFmpeg de linha de comando para a maior parte do trabalho).",
    ),
    # ---- recursos do app --------------------------------------------------- #
    Item(
        item_id="res.qss.dark",
        label="corteia/resources/qss/dark.qss",
        level=REQUIRED,
        patterns=["corteia/resources/qss/dark.qss"],
        hint="ui/theme.py:qss_path() lê esse arquivo; sem ele a janela abre "
             "sem estilo nenhum.",
    ),
    Item(
        item_id="res.qss.light",
        label="corteia/resources/qss/light.qss",
        level=REQUIRED,
        patterns=["corteia/resources/qss/light.qss"],
    ),
    Item(
        item_id="res.i18n.en",
        label="corteia/resources/i18n/en.json",
        level=REQUIRED,
        patterns=["corteia/resources/i18n/en.json"],
        hint="corteia/i18n.py:31 cai de volta no português se o arquivo não "
             "estiver no bundle — trocar de idioma parece não fazer nada.",
    ),
    Item(
        item_id="res.model.yunet",
        label="corteia/resources/models/face_detection_yunet_2023mar.onnx",
        level=REQUIRED,
        patterns=["corteia/resources/models/face_detection_yunet_2023mar.onnx"],
        hint="core/faces.py:39 (YUNET_FILE); sem ele a detecção de rosto cai no "
             "fallback Haar, que não existe no OpenCV 5.",
    ),
    Item(
        item_id="assets.icon",
        label="assets/icon.png (ícone da janela em runtime)",
        level=REQUIRED,
        patterns=["assets/icon.png"],
        hint="ui/app.py:76 e ui/windows/main_window.py:114 procuram "
             "bundle_root()/assets/icon.png; precisa estar em `datas` no spec.",
    ),
    Item(
        item_id="assets.ico",
        label="assets/icon.ico",
        level=OPTIONAL,
        patterns=["assets/icon.ico"],
    ),
    # ---- apoio de runtime -------------------------------------------------- #
    Item(
        item_id="pyz",
        label="arquivo de módulos python (base_library.zip)",
        level=REQUIRED,
        patterns=["base_library.zip"],
        hint="se sumir, o `name=` do COLLECT divergiu do diretório checado.",
    ),
    Item(
        item_id="vcruntime",
        label="VCRUNTIME140.dll no bundle",
        level=OPTIONAL,
        patterns=["VCRUNTIME140.dll", "**/VCRUNTIME140.dll"],
        hint="normalmente vem do Visual C++ Redistributable do sistema; se "
             "não houver, o instalador avisa (installer/corteia.iss -> VCMSG).",
    ),
    Item(
        item_id="ffmpeg",
        label="ffmpeg.exe/ffprobe.exe embutidos (opcional)",
        level=OPTIONAL,
        patterns=["ffmpeg.exe", "bin/ffmpeg.exe", "**/ffmpeg.exe"],
        hint="o app aceita FFmpeg pelo PATH; embutir é só conveniência para "
             "máquina sem winget.",
    ),
]


# --------------------------------------------------------------------------- #
#  Busca
# --------------------------------------------------------------------------- #
def _is_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def search_roots(root: Path) -> list[Path]:
    """PyInstaller >= 6 (one-dir) põe exe na raiz e TODO o resto em `_internal/`.

    Varremos os dois para o mesmo padrão funcionar no layout novo e no antigo
    (<= 5, tudo na raiz). Ordem = prioridade.
    """
    roots = [root]
    for extra in ("_internal", "Contents/Resources"):
        cand = root / extra
        if cand.is_dir():
            roots.append(cand)
    return roots


def _scan(root: Path, item: Item) -> list[Path]:
    """Retorna os arquivos que satisfazem o item (primeiro padrão que acertar)."""
    for base in search_roots(root):
        for pattern in item.patterns:
            try:
                hits = [p for p in sorted(base.glob(pattern))
                        if _is_file(p) and p.stat().st_size >= item.min_bytes]
            except (OSError, ValueError, IndexError):
                continue
            if hits:
                return hits
    return []


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def check(root: Path, item: Item) -> tuple[bool, str, list[str]]:
    """(ok, detalhe, caminhos_encontrados)."""
    hits = _scan(root, item)
    if not hits:
        return False, "0 arquivo(s)", []

    if item.all_of:
        # precisa achar TODAS as famílias, e em lugares não duvidosos
        names = [h.name.lower() for h in hits]
        missing, found = [], {}
        for label, token in item.all_of:
            match = next((h for h in hits if token in h.name.lower()), None)
            if match is None:
                missing.append(label)
            else:
                found[label] = _rel(match, root)
        detail = ", ".join(f"{k}={Path(v).name}" for k, v in sorted(found.items()))
        if missing:
            return False, f"faltando: {', '.join(missing)} | {detail}", []
        where = sorted({os.path.dirname(v) for v in found.values()})
        return True, f"{detail} em {'; '.join(where) or '.'}", [str(h) for h in hits[:4]]

    where = ", ".join(_rel(h, root) for h in hits[:3])
    if len(hits) > 3:
        where += f" (+{len(hits) - 3})"
    return True, f"{len(hits)} arquivo(s): {where}", [str(h) for h in hits[:4]]


# --------------------------------------------------------------------------- #
#  Saída
# --------------------------------------------------------------------------- #
def _safe_print(text: str = "") -> None:
    """Nunca estourar UnicodeEncodeError no `cmd` (cp850/cp1252) do build."""
    out = sys.stdout
    enc = getattr(out, "encoding", None) or "ascii"
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def _row(status: str, level: str, label: str, detail: str,
         widths: tuple[int, int, int]) -> str:
    ws, wl, wd = widths
    return (f"{status:<{ws}} {level:<{wl}} {label:<{wl_ := 0}{wl}} "
            f"{detail}")


def render_table(rows: list[dict], root: Path | None, args) -> int:
    """Imprime a tabela e devolve o código de saída."""
    ws = max([len(r["status"]) for r in rows] + [len("STATUS")])
    wl = len("OBRIGATORIO")
    ll = max([len(r["label"]) for r in rows] + [len("ITEM")])
    sep = "-" * (ws + wl + ll + 44)

    _safe_print("")
    _safe_print("=" * len(sep))
    _safe_print(f"  VERIFICAÇÃO DO PACK — {APP_NAME}")
    if root is not None:
        total = 0
        size = 0
        if root.exists():
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in filenames:
                    total += 1
                    try:
                        size += os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        pass
            _safe_print(f"  diretório : {root}")
            _safe_print(f"  raizes    : {', '.join(_rel(r, root) or '.' for r in search_roots(root))}")
            _safe_print(f"  conteúdo  : {total} arquivo(s), "
                        f"{size / (1024 * 1024):.1f} MiB")
        else:
            _safe_print(f"  diretório : {root} (INEXISTENTE)")
    else:
        _safe_print("  modo      : DRY-RUN (nada é lido do disco)")
    _safe_print("=" * len(sep))
    _safe_print(f"{'STATUS':<{ws}} {'NÍVEL':<{wl}} {'ITEM':<{ll}}  DETALHE")
    _safe_print(sep)
    for r in rows:
        _safe_print(f"{r['status']:<{ws}} {r['level']:<{wl}} "
                    f"{r['label']:<{ll}}  {r['detail']}")
        if args.verbose and r.get("paths"):
            for p in r["paths"]:
                _safe_print(" " * (ws + wl + ll + 5) + f"-> {p}")
        if args.verbose and r["status"].startswith("FALTANDO") and r.get("hint"):
            _safe_print(" " * (ws + wl + ll + 5) + f"dica: {r['hint']}")
    _safe_print(sep)

    miss_req = [r for r in rows if r["status"].startswith("FALTANDO")
                and r["level"] == "OBR"]
    miss_opt = [r for r in rows if r["status"].startswith("FALTANDO")
                and r["level"] != "OBR"]
    ok_n = len([r for r in rows if r["status"].startswith("PRESENTE")])
    _safe_print(f"  PRESENTES ............. {ok_n}/{len(rows)}")
    _safe_print(f"  OBRIGATÓRIOS FALTANDO . {len(miss_req)}")
    for r in miss_req:
        _safe_print(f"      - {r['label']}: {r['hint'] or 'sem dica'}")
    _safe_print(f"  OPCIONAIS FALTANDO .... {len(miss_opt)}")
    _safe_print("")
    if miss_req:
        _safe_print("RESULTADO: FALHOU — o bundle não está pronto para uma "
                    "máquina Windows limpa.")
        _safe_print("           Corrija `corteia.spec` (datas/binaries/"
                    "hiddenimports) e congele de novo.")
        return 1
    _safe_print("RESULTADO: OK para os itens estáticos. Ainda falta o teste "
                "real: docs/WINDOWS.md.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="check_package.py",
        description="Verifica se o bundle one-dir do CorteIA Studio tem as "
                    "DLLs e os recursos que o runtime abre.",
    )
    ap.add_argument("dist", nargs="?", default=DEFAULT_DIST,
                    help=f"diretório do bundle (padrão: {DEFAULT_DIST})")
    ap.add_argument("--dist", dest="dist_opt", default=None,
                    help="alternativa ao argumento posicional")
    ap.add_argument("--dry-run", action="store_true",
                    help="só lista o que seria procurado; não lê o disco e "
                         "sempre sai 0")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="mostra todos os caminhos encontrados e as dicas")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="saída em JSON (para o log do CI)")
    args = ap.parse_args(argv)

    # cmd do Windows passa aspas de forma irregular; aceitar sem aspas também
    raw = args.dist_opt or args.dist
    root = Path(raw)
    if not root.is_absolute():
        # o build_windows.bat roda a partir da raiz do projeto
        root = (Path.cwd() / root).resolve()

    if args.dry_run:
        _safe_print(f"DRY-RUN — diretório que seria verificado: {root}")
        _safe_print(f"{len(ITEMS)} itens:\n")
        for it in ITEMS:
            _safe_print(f"  [{LEVELS[it.level]:>3}] {it.item_id:<22} {it.label}")
            for pat in it.patterns:
                _safe_print(f"          glob: {pat}")
            if it.all_of:
                _safe_print("          exige todas: "
                            + ", ".join(n for n, _ in it.all_of))
        _safe_print("\nDRY-RUN: nada foi lido do disco. exit 0")
        return 0

    if not root.is_dir():
        _safe_print(f"ERRO: '{root}' não é um diretório.")
        _safe_print("      Rode o freeze antes:  pyinstaller --noconfirm "
                    "corteia.spec")
        return 2

    rows = []
    for it in ITEMS:
        ok, detail, paths = check(root, it)
        status = "PRESENTE" if ok else "FALTANDO"
        if not ok and sys.platform != "win32" and it.is_win_item():
            detail += " (esperado: este bundle não é do Windows)"
        rows.append({"id": it.item_id, "status": status,
                     "level": LEVELS[it.level], "label": it.label,
                     "detail": detail, "paths": paths, "hint": it.hint})

    if args.as_json:
        print(json.dumps({"dist": str(root), "platform": sys.platform,
                          "items": rows}, ensure_ascii=False, indent=2))
        missing = [r for r in rows if r["status"] == "FALTANDO"
                   and r["level"] == "OBR"]
        return 1 if missing else 0

    return render_table(rows, root, args)


if __name__ == "__main__":
    raise SystemExit(main())
