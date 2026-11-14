@echo off
rem ===========================================================================
rem  CorteIA Studio - executar no Windows 10/11 (duplo clique ou cmd)
rem
rem      run.bat                 abre o app
rem      run.bat --smoke         inicializa tudo, nao abre janela, sai com 0/1
rem      run.bat --dev           mesmo com logs INFO no console
rem
rem  Se .venv nao existir, chama install.bat primeiro.
rem  Codigo de saida: 0 ok | 1 app/falha | 3 ambiente ausente | 4 usuario cancelou.
rem
rem  Testar no Windows: duplo clique (deve abrir sem console preto);
rem  no cmd: "run.bat --smoke" e depois "echo %errorlevel%".
rem ===========================================================================
setlocal EnableExtensions
title CorteIA Studio

set "ROOT=%~dp0"
pushd "%ROOT%"

set "PY=%ROOT%.venv\Scripts\python.exe"
if exist "%PY%" goto have_python

echo [ERRO] Ambiente nao instalado (.venv\Scripts\python.exe nao existe).
echo        Rode primeiro:  install.bat
if not exist "%ROOT%install.bat" goto no_installer
choice /C SN /M "Instalar agora (usa internet)?"
if errorlevel 2 goto cancelled
call install.bat
if errorlevel 1 goto install_failed
if not exist "%PY%" goto install_failed

:have_python
rem nao imprimir no console quando aberto por duplo clique (janela "python.exe")
echo [*] Iniciando CorteIA Studio...
"%PY%" "%ROOT%run.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [ERRO] O app terminou com codigo %RC%.
    echo        Log: "%%APPDATA%%\CorteIA Studio\logs\corteia.log"
    echo        Solucoes comuns: README.md - "Solucionacao de problemas".
)
popd
endlocal
exit /b %RC%

:no_installer
echo [ERRO] install.bat nao esta na mesma pasta deste script.
popd
endlocal
exit /b 3

:cancelled
echo Instalacao cancelada.
popd
endlocal
exit /b 4

:install_failed
echo [ERRO] A instalacao nao concluiu. Abra install.bat manualmente para ver a causa.
popd
endlocal
exit /b 3
