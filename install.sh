#!/usr/bin/env bash
# ============================================================================
# CorteIA Studio - Linux / macOS installer
#
#   ./install.sh                 create .venv, install deps, verify FFmpeg
#   ./install.sh --check-only    only audit the environment (no install)
#   ./install.sh --with-ffmpeg   also try to install FFmpeg (apt/dnf/pacman/brew)
#   ./install.sh --python BIN    use a specific interpreter (e.g. python3.12)
#   ./install.sh --dev           additionally install requirements-dev.txt
#
# Idempotent: safe to run twice; it reuses .venv and re-checks requirements.
# ============================================================================
set -euo pipefail

CHECK_ONLY=0
WITH_FFMPEG=0
DEV=0
PYTHON_BIN=""
VENV=".venv"

while [ $# -gt 0 ]; do
    case "$1" in
        --check-only)  CHECK_ONLY=1 ;;
        --with-ffmpeg) WITH_FFMPEG=1 ;;
        --dev)         DEV=1 ;;
        --python)      PYTHON_BIN="${2:-}"; shift ;;
        --python=*)    PYTHON_BIN="${1#*=}" ;;
        -h|--help)     sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "opcao desconhecida: $1" >&2; exit 2 ;;
    esac
    shift
done

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
[ -t 1 ] || { RED=""; GRN=""; YLW=""; DIM=""; RST=""; }

ok()   { printf '  %s[ok]%s   %s\n' "$GRN" "$RST" "$1"; }
warn() { printf '  %s[aviso]%s %s\n' "$YLW" "$RST" "$1"; }
bad()  { printf '  %s[erro]%s %s\n' "$RED" "$RST" "$1"; }
step() { printf '\n%s==> %s%s\n' "$DIM" "$1" "$RST"; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# --------------------------------------------------------------------------- #
# 1. interpreter
# --------------------------------------------------------------------------- #
step "Procurando Python 3.11+"
if [ -n "$PYTHON_BIN" ]; then
    PY="$PYTHON_BIN"
else
    PY=""
    for cand in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
    done
fi

PY_OK=0
PY_VER="ausente"
if [ -n "$PY" ] && command -v "$PY" >/dev/null 2>&1; then
    PY_VER="$("$PY" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))' 2>/dev/null || echo "quebrado")"
    if "$PY" -c 'import sys;sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
        PY_OK=1
        ok "Python $PY_VER em $(command -v "$PY")"
    else
        bad "Python $PY_VER e insuficiente (precisa de 3.11+)"
    fi
else
    bad "nenhum Python encontrado"
fi

# --------------------------------------------------------------------------- #
# 2. FFmpeg
# --------------------------------------------------------------------------- #
step "Verificando FFmpeg"
FF_OK=0
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    FF_VER="$(ffmpeg -version 2>/dev/null | head -n1 | cut -d' ' -f3)"
    ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -n1 | cut -d' ' -f1-3)"
    ok "ffprobe $(ffprobe -version 2>/dev/null | head -n1 | cut -d' ' -f3) em $(command -v ffprobe)"
    FF_OK=1
else
    bad "ffmpeg e/ou ffprobe nao estao no PATH"
    if [ "$WITH_FFMPEG" -eq 1 ]; then
        step "Instalando FFmpeg (--with-ffmpeg)"
        if command -v apt-get >/dev/null 2>&1; then
            (sudo -n apt-get update -y && sudo -n apt-get install -y ffmpeg) || \
                warn "rode manualmente: sudo apt install -y ffmpeg"
        elif command -v dnf >/dev/null 2>&1; then
            sudo -n dnf install -y ffmpeg || warn "rode manualmente: sudo dnf install ffmpeg (ou habilite RPM Fusion)"
        elif command -v pacman >/dev/null 2>&1; then
            sudo -n pacman -Sy --noconfirm ffmpeg || warn "rode manualmente: sudo pacman -S ffmpeg"
        elif command -v brew >/dev/null 2>&1; then
            brew install ffmpeg || warn "rode manualmente: brew install ffmpeg"
        else
            warn "gerenciador de pacotes nao reconhecido; instale um build estatico de https://johnvansickle.com/ffmpeg/"
        fi
        command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1 && FF_OK=1
    fi
    if [ "$FF_OK" -eq 0 ]; then
        warn "o CorteIA Studio nao funciona sem FFmpeg. Linux: sudo apt install -y ffmpeg"
        warn "macOS: brew install ffmpeg"
        warn "ou coloque ffmpeg/ffprobe executaveis em third_party/ffmpeg/ (veja README.md)."
    fi
fi

# --------------------------------------------------------------------------- #
# 3. extras that ffmpeg builds often lack
# --------------------------------------------------------------------------- #
step "Recursos do FFmpeg relevantes para o app"
if [ "$FF_OK" -eq 1 ]; then
    # A lista de recursos e capturada UMA vez e comparada como palavra inteira.
    # Encadear `ffmpeg | grep -q` faz o grep fechar o pipe ja no primeiro match:
    # o ffmpeg morre de SIGPIPE (rc 141) e, com `set -o pipefail`, o teste dava
    # falso negativo e imprimia "[erro] sem libx264" num FFmpeg que tem libx264
    # e aac. Agora a lista vira variavel e `grep -qw` busca a palavra exata.
    ENC_LIST="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
    FLT_LIST="$(ffmpeg -hide_banner -filters 2>/dev/null || true)"
    for enc in libx264 libx265 aac; do
        if grep -qw "$enc" <<<"$ENC_LIST"; then
            ok "encoder $enc disponivel"
        else
            case "$enc" in
                libx265) warn "sem libx265: exportacao em H.265 fica indisponivel (use H.264)" ;;
                *)       bad  "sem $enc: exportacao de video/audio pode falhar" ;;
            esac
        fi
    done
    if grep -qw subtitles <<<"$FLT_LIST"; then
        ok "filtro subtitles disponivel (legendas queimadas)"
    else
        warn "sem filtro subtitles: use 'legenda lateral/SRT' ou instale um build com libass"
    fi
    if grep -qw h264_nvenc <<<"$ENC_LIST"; then
        ok "h264_nvenc disponivel (encoder NVIDIA)"
    else
        warn "sem h264_nvenc: exportacao sera por CPU (mais lenta, mesma qualidade)"
    fi
fi

# --------------------------------------------------------------------------- #
# 4. optional: disk + RAM report
# --------------------------------------------------------------------------- #
step "Recursos da maquina"
FREE_GB="$({ df -Pk "$ROOT" 2>/dev/null | awk 'NR==2{printf "%.1f", $4/1048576}'; } || echo '?')"
ok "espaco livre no disco do projeto: ${FREE_GB} GB (recomendado >= 10 GB)"
if command -v free >/dev/null 2>&1; then
    RAM_GB="$(free -g | awk '/^Mem:/{print $2}')"
    ok "RAM total: ${RAM_GB} GB"
elif command -v sysctl >/dev/null 2>&1; then
    :
fi
CPU="$( (nproc 2>/dev/null) || (sysctl -n hw.ncpu 2>/dev/null) || echo '?' )"
ok "threads logicas: $CPU"

# --------------------------------------------------------------------------- #
# 5. report / exit for --check-only
# --------------------------------------------------------------------------- #
if [ "$CHECK_ONLY" -eq 1 ]; then
    step "Relatorio (--check-only: nada foi instalado)"
    printf '  Python 3.11+ ............ %s\n' "$([ "$PY_OK" -eq 1 ] && echo 'sim' || echo "nao ($PY_VER)")"
    printf '  FFmpeg/FFprobe .......... %s\n' "$([ "$FF_OK" -eq 1 ] && echo 'no PATH' || echo 'NAO ENCONTRADO')"
    printf '  .venv atual ............. %s\n' "$([ -d "$ROOT/$VENV" ] && echo 'existe' || echo 'nao existe')"
    if [ -d "$ROOT/$VENV" ]; then
        if [ -x "$ROOT/$VENV/bin/python" ]; then
            VENV_VER="$("$ROOT/$VENV/bin/python" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')"
            printf '  .venv Python ............ %s\n' "$VENV_VER"
            for mod in PySide6 faster_whisper cv2 scenedetect openai yt_dlp psutil PIL numpy av; do
                if "$ROOT/$VENV/bin/python" -c "import $mod" >/dev/null 2>&1; then
                    printf '    %-16s ok\n' "$mod"
                else
                    printf '    %-16s FALTANDO\n' "$mod"
                fi
            done
        fi
    fi
    if [ "$PY_OK" -eq 1 ] && [ "$FF_OK" -eq 1 ]; then
        printf '\n%sAmbiente OK. Rode ./install.sh para instalar.%s\n' "$GRN" "$RST"
        exit 0
    fi
    printf '\n%sAmbiente incompleto - corrija os itens [erro] acima.%s\n' "$RED" "$RST"
    exit 1
fi

# --------------------------------------------------------------------------- #
# 6. venv + deps
# --------------------------------------------------------------------------- #
if [ "$PY_OK" -ne 1 ]; then
    bad "instale um Python 3.11+ antes (ex.: sudo apt install python3.12 python3.12-venv)"
    exit 1
fi

step "Criando/atualizando $VENV"
if [ ! -x "$ROOT/$VENV/bin/python" ]; then
    "$PY" -m venv "$ROOT/$VENV"
else
    echo "  .venv ja existe, reutilizando"
fi
# shellcheck disable=SC1091
. "$ROOT/$VENV/bin/activate"

step "Atualizando pip/setuptools/wheel"
python -m pip install --upgrade pip setuptools wheel

step "Instalando dependencias (pode levar alguns minutos)"
python -m pip install -r requirements.txt
if [ "$DEV" -eq 1 ]; then
    python -m pip install -r requirements-dev.txt
fi

step "Instalando o proprio pacote em modo editavel (comando 'corteia')"
python -m pip install -e . --no-deps || warn "pip install -e . falhou; use ./run.sh"

step "Verificacao final"
python - <<'PY'
import importlib, sys
mods = ["PySide6", "faster_whisper", "cv2", "scenedetect", "numpy", "openai",
        "yt_dlp", "psutil", "PIL", "av", "platformdirs"]
missing = [m for m in mods if importlib.util.find_spec(m) is None]
for m in mods:
    print(f"  {'ok  ' if m not in missing else 'FALTA'} {m}")
if missing:
    print("Dependencias ausentes:", ", ".join(missing))
    sys.exit(1)
import cv2
print(f"  OpenCV {cv2.__version__} | YuNet: "
      f"{'sim' if hasattr(cv2, 'FaceDetectorYN') else 'nao'} | Haar: "
      f"{'sim' if hasattr(cv2, 'CascadeClassifier') else 'nao (OpenCV 5)'}")
PY

cat <<EOF

$(printf '\033[32m')Instalacao concluida.$(printf '\033[0m')

Para executar:
    ./run.sh
ou, com o venv ativo:
    . .venv/bin/activate && python run.py

Testes:
    . .venv/bin/activate && pytest -q

Build de Linux (one-dir, nao requer Windows):
    ./build_linux.sh --dry-run      # valida spec/caminhos sem congelar
    ./build_linux.sh                # gera dist/CorteIA Studio/
EOF
