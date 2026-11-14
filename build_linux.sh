#!/usr/bin/env bash
# ============================================================================
# CorteIA Studio - build de Linux/macOS (PyInstaller one-dir)
#
#   ./build_linux.sh                gera dist/CorteIA Studio/ (requisito: rede)
#   ./build_linux.sh --dry-run      só mostra o plano; não instala nem congela
#   ./build_linux.sh --no-venv      usa o Python/PYInstaller já ativos
#   ./build_linux.sh --clean        apaga build/, dist/ e .spec cache antes
#
# O resultado é uma PASTA, não um arquivo único: faster-whisper, CTranslate2 e
# Qt carregam .so/.dylib em tempo de execução. Distribua o conteúdo de
# dist/CorteIA Studio/ inteiro (zip, AppImage, .deb por fora deste script).
#
# NÃO testado neste ambiente de referência: aqui não há PyInstaller instalado,
# então o que se valida com `bash -n build_linux.sh` é a sintaxe, e o que se
# valida com --dry-run é o plano (caminhos, spec, recursos). Documentado em
# README.md -> Limitações.
# ============================================================================
set -e
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV=".venv"
APP_NAME="CorteIA Studio"
SPEC="corteia.spec"
DRY_RUN=0
USE_VENV=1
DO_CLEAN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --no-venv) USE_VENV=0 ;;
        --clean)   DO_CLEAN=1 ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "opcao desconhecida: $1" >&2; exit 2 ;;
    esac
    shift
done

say()  { printf '\033[2m==>\033[0m %s\n' "$1"; }
ok()   { printf '    \033[32m[ok]\033[0m %s\n' "$1"; }
warn() { printf '    \033[33m[aviso]\033[0m %s\n' "$1"; }
die()  { printf '    \033[31m[erro]\033[0m %s\n' "$1" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# plano
# --------------------------------------------------------------------------- #
say "Plano de build"
echo "    projeto ........ $ROOT"
echo "    spec ........... $SPEC"
echo "    saida .......... dist/$APP_NAME/ (one-dir)"
echo "    icone .......... assets/icon.ico"
echo "    instalador(?) .. installer/corteia.iss só no Windows (build_windows.bat)"

# Recursos: o spec pula o que não existir, então avisamos aqui também.
for sub in qss models i18n icons; do
    if [ -d "corteia/resources/$sub" ]; then
        ok "recurso corteia/resources/$sub"
    else
        warn "corteia/resources/$sub não existe — o spec vai pular"
    fi
done
[ -f assets/icon.ico ] && ok "assets/icon.ico" || warn "assets/icon.ico ausente (exe sem ícone)"
[ -f "$SPEC" ] || die "$SPEC não encontrado"
[ -f requirements-build.txt ] || die "requirements-build.txt não encontrado"
command -v git >/dev/null 2>&1 && \
    echo "    commit ......... $(git rev-parse --short HEAD 2>/dev/null || echo 'sem git')"

if [ "$DRY_RUN" -eq 1 ]; then
    say "--dry-run: nada será instalado nem congelado. Plano acima; comandos que rodariam:"
    if [ "$USE_VENV" -eq 1 ]; then
        echo "      python3 -m venv $VENV"
        echo "      . $VENV/bin/activate"
    fi
    echo "      python -m pip install --upgrade pip setuptools wheel"
    echo "      python -m pip install -r requirements-build.txt   # traz pyinstaller"
    echo "      python -m PyInstaller --noconfirm $SPEC"
    echo "      ls -la dist/'$APP_NAME'/        # verificação final"
    exit 0
fi

# --------------------------------------------------------------------------- #
# ambiente
# --------------------------------------------------------------------------- #
if [ "$DO_CLEAN" -eq 1 ]; then
    say "Limpando artefatos anteriores"
    rm -rf build dist "$APP_NAME.spec" __pycache__ 2>/dev/null || true
    ok "build/ e dist/ removidos"
fi

if [ "$USE_VENV" -eq 1 ]; then
    say "Ambiente virtual ($VENV)"
    if [ ! -x "$VENV/bin/python" ]; then
        PY=""
        for cand in python3.12 python3.11 python3; do
            command -v "$cand" >/dev/null 2>&1 && { PY="$cand"; break; }
        done
        [ -n "$PY" ] || die "nenhum Python 3.11+ no PATH"
        "$PY" -m venv "$VENV"
        ok "venv criado com $PY"
    else
        ok "venv reaproveitado"
    fi
    # shellcheck disable=SC1091
    . "$VENV/bin/activate"
else
    say "Usando o Python já ativo"
    command -v python >/dev/null 2>&1 || die "python não encontrado"
fi

python - <<'PY' || die "Python < 3.11: o app exige 3.11+"
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

say "Instalando dependências de build (pode demorar)"
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements-build.txt
ok "requirements-build.txt instalado"

# PyInstaller pode vir de requirements-build.txt ou do sistema.
if python -c "import PyInstaller" >/dev/null 2>&1; then
    ok "PyInstaller $(python -m PyInstaller --version 2>/dev/null | tail -1)"
else
    die "PyInstaller indisponível mesmo após o pip install"
fi

command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg no PATH (bom: o build checa encoders)" \
    || warn "ffmpeg ausente — o bundle funciona, mas só roda onde o FFmpeg existir"

# --------------------------------------------------------------------------- #
# congelar
# --------------------------------------------------------------------------- #
say "Congelando com o PyInstaller (one-dir)"
rm -rf "dist/$APP_NAME" build/"$APP_NAME"
python -m PyInstaller --noconfirm --log-level WARN "$SPEC"
ok "PyInstaller terminou"

# --------------------------------------------------------------------------- #
# verificação final
# --------------------------------------------------------------------------- #
say "Verificação final: conteúdo de dist/"
[ -d dist ] || die "dist/ não foi gerado"
ls -la dist/

EXE="dist/$APP_NAME/$APP_NAME"
if [ ! -f "$EXE" ]; then
    # macOS usa o mesmo nome; se algo mudou, listamos o que existe em vez de
    # fingir sucesso.
    find "dist/$APP_NAME" -maxdepth 1 -type f -perm -u+x | head -10
    die "executável esperado não existe: $EXE"
fi
ok "executável: $EXE"

echo
echo "    bibliotecas de terceiros no bundle:"
for lib in libpython _ssl libx264 ctranslate2 onnxruntime qt6multimedia av; do
    if find "dist/$APP_NAME" -iname "*${lib}*" -print -quit 2>/dev/null | grep -q .; then
        ok "encontrado *${lib}*"
    else
        warn "não encontrado *${lib}* (pode ser normal: o nome real varia por SO)"
    fi
done

TOTAL="$(du -sh "dist/$APP_NAME" 2>/dev/null | cut -f1)"
ok "tamanho total do bundle: ${TOTAL:-?}"

echo
echo "Teste rápido (sem abrir janela):"
echo "    \"$EXE\" --check     # deve imprimir FFmpeg/codecs/fontes e sair 0"
echo "    \"$EXE\" --smoke     # pipeline real + exportação"
echo
echo "Para distribuir: zipar o conteúdo de dist/$APP_NAME/ (a pasta inteira),"
echo "ou usar o instalador nativo (AppImage/.deb) por fora deste script."
echo "No Windows, use build_windows.bat em vez disto."
