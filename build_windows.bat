@echo off
rem ===========================================================================
rem  CorteIA Studio - build do instalador Windows
rem
rem      build_windows.bat                 freeze + .exe do Inno Setup
rem      build_windows.bat --portable      so o PyInstaller (sem Inno Setup)
rem      build_windows.bat --clean         apaga build\ e dist\ antes
rem
rem  Requisito: Python 3.11+, internet (pip) e Inno Setup 6 para o instalador.
rem  Codigo de saida: 0 ok | 1 falha | 2 ambiente ausente.
rem
rem  NAO EXECUTADO no ambiente de referencia deste build (Linux, sem Windows,
rem  sem pyinstaller, sem iscc). A sintaxe foi revista; rode numa maquina
rem  Windows real e confira a lista de verificacao de docs\WINDOWS.md.
rem ===========================================================================
setlocal EnableExtensions
title CorteIA Studio - build
set "ROOT=%~dp0"
pushd "%ROOT%"
set "APPNAME=CorteIA Studio"
set "SPEC=corteia.spec"
set "ISS=installer\corteia.iss"
set "VENV=.venv"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "PORTABLE=0"
set "DO_CLEAN=0"
set "NO_DOWNLOAD=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--portable" (set "PORTABLE=1" & shift & goto parse_args)
if /I "%~1"=="--clean" (set "DO_CLEAN=1" & shift & goto parse_args)
if /I "%~1"=="--no-download" (set "NO_DOWNLOAD=1" & shift & goto parse_args)
if /I "%~1"=="-h" goto usage
if /I "%~1"=="--help" goto usage
echo [ERRO] Parametro desconhecido: %~1
goto usage
:args_done

echo ==============================================================
echo   CORTEIA STUDIO - build do instalador Windows
echo ==============================================================

rem ---------------------------------------------------------------- Python ---
if not exist "%PY%" goto create_venv
echo [*] Ambiente .venv reaproveitado
goto have_python

:create_venv
echo [*] Criando o ambiente virtual...
where py >nul 2>nul
if not errorlevel 1 (
    py -3.12 -m venv "%VENV%" 2>nul
    if not exist "%PY%" py -3 -m venv "%VENV%"
) else (
    where python >nul 2>nul || goto no_python
    python -m venv "%VENV%"
)
if not exist "%PY%" goto no_python

:have_python
"%PY%" -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [ERRO] O Python do .venv e mais velho que 3.11.
    echo        Apague a pasta .venv e rode de novo com um Python mais novo.
    goto fail
)

if "%DO_CLEAN%"=="1" (
    echo [*] Limpando build\ e dist\...
    if exist build rmdir /s /q build
    if exist dist rmdir /s /q dist
)

if not exist "%SPEC%" (
    echo [ERRO] "%SPEC%" nao existe nesta pasta.
    goto fail
)

rem ------------------------------------------------------- dependencias -------
echo [*] Instalando dependencias de build ^(pode demorar^)...
"%PY%" -m pip install --upgrade pip setuptools wheel >nul
if errorlevel 1 goto pip_fail
"%PY%" -m pip install -r requirements-build.txt
if errorlevel 1 goto pip_fail

"%PY%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [ERRO] PyInstaller nao foi instalado. Rode manualmente:
    echo        "%PY%" -m pip install -r requirements-build.txt
    goto fail
)

rem ------------------------------------------------------------- freeze -------
echo.
echo [*] Congelando com PyInstaller ^(one-dir^)...
"%PY%" -m PyInstaller --noconfirm --log-level WARN "%SPEC%"
if errorlevel 1 (
    echo [ERRO] O PyInstaller falhou. Leia as ultimas linhas acima e o log em build\.
    goto fail
)

if not exist "dist\%APPNAME%" (
    echo [ERRO] dist\%APPNAME% nao foi gerado.
    goto fail
)
echo [*] Bundle gerado em dist\%APPNAME%

rem ---------------------------------------- VERIFICACAO AUTOMATICA DO PACK ----
rem O PyInstaller "terminar bem" nao prova nada: se faltar qwindows.dll, a
rem ctranslate2.dll ou os .qss do tema, o programa morre MUDO na maquina do
rem cliente (console=False nao mostra erro nenhum). check_package.py confere
rem peca por peca e sai com codigo != 0 se faltar algo obrigatorio.
echo.
echo [*] Verificando o conteudo do bundle...
if not exist "scripts\check_package.py" (
    echo [ERRO] scripts\check_package.py nao existe; nao da para validar o pack.
    goto fail
)
"%PY%" scripts\check_package.py "dist\%APPNAME%"
if errorlevel 1 (
    echo.
    echo [ERRO] O bundle esta INCOMPLETO. Corrija corteia.spec e congele de novo.
    echo        Detalhe:  "%PY%" scripts\check_package.py "dist\%APPNAME%" -v
    goto fail
)
echo [*] Bundle completo: todos os itens obrigatorios presentes.
if "%PORTABLE%"=="1" (
    echo.
    echo Feito. Para distribuir o portable, zipar o conteudo de dist\%APPNAME%\ inteiro.
    goto done
)

rem ------------------------------------------------------------ iscc ---------
echo.
echo [*] Procurando o Inno Setup ^(iscc.exe^)...
where iscc >nul 2>nul
if not errorlevel 1 goto run_iscc

rem Caminhos-padrao de instalacao do Inno Setup 6.
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" goto run_iscc_path
set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" goto run_iscc_path

if "%NO_DOWNLOAD%"=="1" goto iscc_missing

rem --------------------------------------------- fallback: baixar o Inno -----
rem URL OFICIAL do instalador do Inno Setup 6. /CURRENTUSER instala sem pedir
rem admin. Use --no-download para desligar este passo (ambiente corporativo sem
rem internet, ou build reprodutivel).
echo [*] Inno Setup nao encontrado. Baixando o instalador oficial...
set "ISMSI=%TEMP%\innosetup-corteia.exe"
curl -L -o "%ISMSI%" "https://jrsoftware.org/download.php/is.exe"
if errorlevel 1 (
    echo [ERRO] O download falhou (sem internet? proxy?).
    goto iscc_missing
)
echo [*] Instalando o Inno Setup em silencio (somente este usuario)...
"%ISMSI%" /VERYSILENT /SUPPRESSLICENSES /NORESTART /CURRENTUSER
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" (
    echo [*] Inno Setup instalado: %ISCC%
    goto run_iscc_with
)
set "ISCC="
where iscc >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%I in ('where iscc') do if not defined ISCC set "ISCC=%%I"
    goto run_iscc_with
)

:iscc_missing
echo.
echo [ERRO] Inno Setup nao esta instalado - nao da para gerar o instalador.
echo.
echo        Baixe o Inno Setup 6 ^(gratis^) em:
echo            https://jrsoftware.org/isdl.php
echo        Instale com as opcoes padrao e rode este script de novo.
echo        Sem reiniciar o Windows, feche e reabra o cmd para o PATH novo valer.
echo        (este script tambem baixa sozinho: rode sem --no-download)
echo.
echo        Atalho: depois de instalar, "where iscc" deve responder.
echo.
echo        O bundle congelado ja esta pronto e ja foi VERIFICADO
echo        (check_package.py) e pode ser distribuido como pasta zipada:
echo            build_windows.bat --portable
goto fail_ish

:run_iscc_path
echo [*] Inno Setup encontrado: %ISCC%
goto run_iscc_with

:run_iscc
for /f "delims=" %%I in ('where iscc') do if not defined ISCC set "ISCC=%%I"
echo [*] Inno Setup no PATH: %ISCC%

:run_iscc_with
if not exist "%ISS%" (
    echo [ERRO] "%ISS%" nao existe. O roteiro do instalador desapareceu?
    goto fail
)
echo [*] Compilando o instalador...
"%ISCC%" "%ISS%"
if errorlevel 1 (
    echo [ERRO] O Inno Setup retornou erro. Confira as linhas acima.
    goto fail
)

echo.
echo ===============================================================
echo   FEITO. Instalador em:
echo       dist\CorteIA-Setup-1.0.0.exe
echo
echo   Antes de distribuir, faca o teste numa maquina Windows limpa:
echo       docs\WINDOWS.md  -  "Checklist de maquina limpa"
echo ===============================================================

:done
popd
endlocal
exit /b 0

:pip_fail
echo [ERRO] O pip falhou. Provavelmente sem internet ou antivirus bloqueando.
goto fail

:no_python
echo [ERRO] Nenhum Python 3.11+ encontrado. Instale de https://www.python.org/downloads/
echo        marcando "Add python.exe to PATH".
goto fail_ish

:fail
popd
endlocal
exit /b 1

:fail_ish
popd
endlocal
exit /b 2

:usage
echo Uso: build_windows.bat [--portable] [--clean] [--no-download]
echo        --portable     so o PyInstaller, sem Inno Setup
echo        --clean        apaga build\ e dist\ antes de comecar
echo        --no-download  nao baixar o Inno Setup automaticamente quando ele
echo                       nao estiver instalado (para com erro em vez disso)
echo.
echo A verificacao do pack (scripts\check_package.py) sempre roda depois do
echo freeze e bloqueia o build se faltar DLL ou recurso obrigatorio.
popd
endlocal
exit /b 0
