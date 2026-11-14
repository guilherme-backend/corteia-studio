#!/usr/bin/env bash
# ============================================================================
#  CorteIA Studio - executar em Linux/macOS
#
#      ./run.sh              abre o app
#      ./run.sh --smoke      inicializa tudo sem abrir janela (exit 0/1)
#      ./run.sh --dev        logs INFO no console
#
#  Se .venv nao existir, oferece rodar install.sh.
# ============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
    echo "[ERRO] Ambiente nao instalado (.venv/bin/python nao existe)."
    echo "       Rode primeiro:  ./install.sh"
    if [ -f "$ROOT/install.sh" ] && [ -t 0 ]; then
        read -r -p "Instalar agora (usa internet)? [s/N] " ans
        case "${ans:-s}" in
            [sS]*) bash "$ROOT/install.sh" || { echo "[ERRO] Instalacao falhou."; exit 3; } ;;
            *) echo "Cancelado."; exit 4 ;;
        esac
    else
        exit 3
    fi
fi

echo "[*] Iniciando CorteIA Studio..."
"$PY" "$ROOT/run.py" "$@"
rc=$?
if [ "$rc" -ne 0 ]; then
    echo
    echo "[ERRO] O app terminou com codigo $rc."
    echo "       Log: ~/.config/corteia-studio/logs/corteia.log"
    echo "       Solucoes comuns: README.md - 'Solucionacao de problemas'."
fi
exit "$rc"
