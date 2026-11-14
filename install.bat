@echo off
rem ===========================================================================
rem  CorteIA Studio - instalador Windows 10/11 (cmd.exe)
rem
rem  Uso:
rem      install.bat                    cria .venv, instala dependencias, confere FFmpeg
rem      install.bat --check-only       apenas audita o ambiente (nao instala nada)
rem      install.bat --with-ffmpeg      tenta instalar o FFmpeg via winget se faltar
rem      install.bat --dev              instala tambem requirements-dev.txt (pytest/ruff)
rem
rem  Idempotente: pode rodar duas vezes, reaproveita o .venv existente.
rem  Codigo de saida: 0 = ok | 1 = erro | 2 = parametro invalido.
rem
rem  ONDE TESTAR NO WINDOWS: pasta do projeto, no "cmd" (PowerShell funciona
rem  tambem: .\install.bat). Sequencia recomendada:
rem      install.bat --check-only   -> deve reportar Python/FFmpeg sem instalar
rem      install.bat                -> instalacao completa
rem      install.bat                -> 2a execucao deve dizer "Reaproveitando"
rem      install.bat --with-ffmpeg  -> numa maquina limpa sem FFmpeg
rem
rem  Duas escolhas deliberadas de sintaxe:
rem   * Texto sem acentos: cmd.exe imprime na codepage 850/437 e "versao"
rem     viraria "versÃ£o". Os acentos ficam no README.md (UTF-8).
rem   * Sem blocos "( ... )" aninhados com echo de variaveis e sem "cmd1 & cmd2"
rem     dentro de "if": "&" separa comandos ANTES do if, entao o segundo
rem     comando rodaria sempre. Argumentos usam um bloco por linha.
rem ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
title CorteIA Studio - instalacao

set "ROOT=%~dp0"
pushd "%ROOT%"

set "VENV=.venv"
set "PYCMD="
set "CHECK_ONLY=0"
set "WITH_FFMPEG=0"
set "DEV=0"
set "FFMPEG_STATE=missing"
set "FFLINE="
set "HAS_X264=0"
set "HAS_X265=0"
set "HAS_SUBS=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--check-only" (set "CHECK_ONLY=1" & shift & goto parse_args)
if /I "%~1"=="--with-ffmpeg" (set "WITH_FFMPEG=1" & shift & goto parse_args)
if /I "%~1"=="--dev" (set "DEV=1" & shift & goto parse_args)
if /I "%~1"=="-h" goto usage
if /I "%~1"=="--help" goto usage
echo [ERRO] Parametro desconhecido: %~1
goto usage

:args_done
echo.
echo ==============================================================
echo   CORTEIA STUDIO 1.0.0 - instalacao Windows
echo ==============================================================

rem ---------------------------------------------------------------- Python ---
call :find_python
if errorlevel 1 goto fail_python

echo.
echo [*] Python: %PYCMD%
"%PYCMD%" --version
rem exige 3.11+ (dataclass slots, "X | Y" em anotacoes, TypeAlias)
"%PYCMD%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 goto fail_python_old

rem --------------------------------------------------------------- FFmpeg ---
where ffmpeg >nul 2>nul
if errorlevel 1 goto ffmpeg_check_bundle
where ffprobe >nul 2>nul
if errorlevel 1 goto ffmpeg_check_bundle
set "FFMPEG_STATE=path"
for /f "delims=" %%V in ('ffmpeg -version 2^>nul') do if not defined FFLINE set "FFLINE=%%V"
echo [*] FFmpeg no PATH: %FFLINE%
goto ffmpeg_capabilities

:ffmpeg_check_bundle
if not exist "%ROOT%third_party\ffmpeg\ffmpeg.exe" goto ffmpeg_missing
if not exist "%ROOT%third_party\ffmpeg\ffprobe.exe" goto ffmpeg_missing
set "FFMPEG_STATE=bundle"
echo [*] FFmpeg no projeto: %ROOT%third_party\ffmpeg  ^(sera copiado para o build^)
for /f "delims=" %%V in ('"%ROOT%third_party\ffmpeg\ffmpeg.exe" -version 2^>nul') do if not defined FFLINE set "FFLINE=%%V"
echo [*] %FFLINE%
goto ffmpeg_capabilities

:ffmpeg_missing
echo.
echo [ERRO] FFmpeg nao encontrado ^(nem no PATH, nem em third_party\ffmpeg^).
if "%WITH_FFMPEG%"=="1" goto try_winget
goto ffmpeg_instructions

:try_winget
where winget >nul 2>nul
if errorlevel 1 (
    echo [ERRO] winget nao esta disponivel nesta maquina. Use as instrucoes manuais.
    goto ffmpeg_instructions
)
echo.
echo [*] Instalando FFmpeg via winget (Gyan.FFmpeg)...
winget install --id Gyan.FFmpeg -e --source winget --accept-source-agreements --accept-package-agreements
if not errorlevel 1 goto winget_ok
echo [ERRO] winget retornou erro. Instalacao manual abaixo.
goto ffmpeg_instructions

:winget_ok
echo.
echo [AVISO] O PATH do sistema so vale para TERMINAIS NOVOS.
echo         Feche este cmd, abra outro e rode "ffmpeg -version".
echo         Para nao depender do PATH: copie ffmpeg.exe e ffprobe.exe para
echo         %ROOT%third_party\ffmpeg\
where ffmpeg >nul 2>nul
if errorlevel 1 goto ffmpeg_instructions
set "FFMPEG_STATE=path"
goto ffmpeg_capabilities

:ffmpeg_capabilities
rem um build "full" do gyan.dev tem os tres; builds "essentials" podem nao ter libx265
ffmpeg -hide_banner -encoders >"%TEMP%\corteia_enc.txt" 2>&1
findstr /C:"libx264" "%TEMP%\corteia_enc.txt" >nul 2>nul
if not errorlevel 1 set "HAS_X264=1"
findstr /C:"libx265" "%TEMP%\corteia_enc.txt" >nul 2>nul
if not errorlevel 1 set "HAS_X265=1"
del "%TEMP%\corteia_enc.txt" >nul 2>nul
ffmpeg -hide_banner -filters >"%TEMP%\corteia_fil.txt" 2>&1
findstr /C:"subtitles" "%TEMP%\corteia_fil.txt" >nul 2>nul
if not errorlevel 1 set "HAS_SUBS=1"
del "%TEMP%\corteia_fil.txt" >nul 2>nul

if "%HAS_X264%"=="1" (echo [*] Encoder H.264 ............. ok) else (echo [ERRO] Sem libx264: o FFmpeg esta incompleto. Baixe "ffmpeg-master-latest-win64-gpl.zip" em https://www.gyan.dev/ffmpeg/builds/)
if "%HAS_X265%"=="1" (echo [*] Encoder H.265 ^(HEVC^) ...... ok) else (echo [AVISO] Sem libx265: exportacao HEVC indisponivel - use H.264.)
if "%HAS_SUBS%"=="1" (echo [*] Filtro subtitles ^(libass^) .. ok) else (echo [AVISO] Sem filtro subtitles: o app ainda grava ASS nativo; legendas queimadas podem falhar.)
goto after_ffmpeg

:ffmpeg_instructions
echo.
echo   Como instalar o FFmpeg no Windows - escolha UMA opcao:
echo.
echo   1) winget  (recomendado)
echo         winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
echo      FECHE e REABRA o terminal e confira:
echo         ffmpeg -version
echo.
echo   2) Chocolatey:   choco install ffmpeg -y
echo      Scoop:        scoop install ffmpeg
echo.
echo   3) Manual: baixe "ffmpeg-master-latest-win64-gpl.zip" de
echo         https://www.gyan.dev/ffmpeg/builds/
echo      descompacte em C:\ffmpeg e adicione C:\ffmpeg\bin ao Path do sistema:
echo         Win+R  -  sysdm.cpl  -  Avancado  -  Variaveis de ambiente  -  Path  -  Novo
echo         (ou rode install.bat --with-ffmpeg, que chama o winget sozinho)
echo      Sem mexer no PATH tambem funciona: copie ffmpeg.exe e ffprobe.exe para
echo         %ROOT%third_party\ffmpeg\
echo         (o app procura primeiro la; veja third_party\ffmpeg\README.txt)
echo.
if "%CHECK_ONLY%"=="1" goto check_report
goto fail_ffmpeg

:after_ffmpeg
rem ------------------------------------------------------------- check-only ---
if "%CHECK_ONLY%"=="1" goto check_report

rem ------------------------------------------------------------------- venv ---
echo.
if not exist "%VENV%\Scripts\python.exe" goto create_venv
echo [*] Reaproveitando o ambiente .venv existente
goto activate_venv

:create_venv
echo [*] Criando ambiente virtual em .venv ...
"%PYCMD%" -m venv "%VENV%"
if errorlevel 1 goto fail_venv

:activate_venv
call "%VENV%\Scripts\activate.bat"
if not exist "%VENV%\Scripts\python.exe" goto fail_venv
echo [*] Ambiente ativo: %ROOT%%VENV%
"%VENV%\Scripts\python.exe" --version

echo.
echo [*] Atualizando pip / setuptools / wheel ...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto fail_pip

echo.
echo [*] Instalando dependencias (requirements.txt) - 5 a 10 minutos na 1a vez...
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail_pip

if not "%DEV%"=="1" goto skip_dev
echo.
echo [*] Instalando ferramentas de desenvolvimento (requirements-dev.txt)...
"%VENV%\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 goto fail_pip
:skip_dev

echo.
echo [*] Instalando o pacote em modo editavel (comando "corteia")...
"%VENV%\Scripts\python.exe" -m pip install -e . --no-deps
if errorlevel 1 echo [AVISO] "pip install -e ." falhou - use run.bat, o app continua funcionando.

echo.
echo [*] Validando importacoes...
"%VENV%\Scripts\python.exe" -c "import importlib,sys;mods=['PySide6','faster_whisper','cv2','scenedetect','numpy','openai','yt_dlp','psutil','PIL','av'];bad=[m for m in mods if importlib.util.find_spec(m) is None];print('  FALTANDO: '+', '.join(bad)) if bad else print('  todas as dependencias importam');sys.exit(1 if bad else 0)"
if errorlevel 1 goto fail_import

"%VENV%\Scripts\python.exe" -c "import cv2;print('  OpenCV '+cv2.__version__+' | YuNet: '+('sim' if hasattr(cv2,'FaceDetectorYN') else 'nao')+' | Haar: '+('sim' if hasattr(cv2,'CascadeClassifier') else 'nao existe no OpenCV 5'))"

echo.
echo ==============================================================
echo   INSTALACAO CONCLUIDA
echo ==============================================================
echo.
echo   Para abrir o app:        run.bat            (duplo clique)
echo   Alternativa no cmd:      .venv\Scripts\activate  -  python run.py
echo   Testes:                  python -m pytest -q
echo.
echo   Na primeira transcricao o modelo Whisper e baixado para
echo     %APPDATA%\CorteIA Studio\models      (75 MB a 3 GB, conforme o modelo)
echo   Config/projetos:         %APPDATA%\CorteIA Studio
echo   Temp de render:          %LOCALAPPDATA%\CorteIA Studio\Temp
echo.
echo   Gerar o .exe:            build_windows.bat
echo   Criar instalador:        ver README.md - secao "Compilar o .exe"
echo.
popd
endlocal
exit /b 0

rem ============================================================ rotinas =====
:find_python
rem preferencia: lancador "py" (nao cai no stub da Microsoft Store que abre a Loja)
set "PYCMD="
for %%V in (3.13 3.12 3.11) do (
    if not defined PYCMD (
        py -%%V -c "import sys" >nul 2>nul && set "PYCMD=py -%%V"
    )
)
if defined PYCMD goto fp_ok
where python >nul 2>nul
if errorlevel 1 goto fp_missing
python -c "import sys" >nul 2>nul
if errorlevel 1 goto fp_missing
set "PYCMD=python"
:fp_ok
exit /b 0

:fp_missing
exit /b 1

:check_report
echo.
echo ==============================================================
echo   RELATORIO --check-only  (nada foi instalado)
echo ==============================================================
echo   Python ...................... %PYCMD%
"%PYCMD%" --version 2>nul
echo   FFmpeg ...................... %FFMPEG_STATE%
echo   encoder H.264 ............... %HAS_X264%   (1 = presente)
echo   encoder H.265 ............... %HAS_X265%
echo   filtro subtitles ............ %HAS_SUBS%
echo   .venv .......................
if not exist "%VENV%\Scripts\python.exe" (
    echo     nao existe - rode: install.bat
    goto check_report_tail
)
"%VENV%\Scripts\python.exe" --version
"%VENV%\Scripts\python.exe" -c "import importlib;mods=['PySide6','faster_whisper','cv2','scenedetect','numpy','openai','yt_dlp','psutil','PIL','av'];print('     '+', '.join(m+(' ok' if importlib.util.find_spec(m) else ' FALTANDO') for m in mods))"
:check_report_tail
echo   third_party\ffmpeg ..........
if exist "%ROOT%third_party\ffmpeg\ffmpeg.exe" (echo     ffmpeg.exe presente) else (echo     vazio - o app usara o FFmpeg do PATH)
echo.
if not "%FFMPEG_STATE%"=="missing" (
    echo   Ambiente pronto. Rode: install.bat
    popd
    endlocal
    exit /b 0
)
echo   Ambiente incompleto: instale o FFmpeg e rode de novo.
popd
endlocal
exit /b 1

rem ============================================================== falhas ====
:usage
echo Uso: install.bat [--check-only] [--with-ffmpeg] [--dev]
popd
endlocal
exit /b 2

:fail_python
echo.
echo [ERRO] Python nao encontrado.
echo        Instale o Python 3.12:  https://www.python.org/downloads/
echo        marque "Add python.exe to PATH", ou:
echo            winget install --id Python.Python.3.12 -e
echo        Depois abra um novo terminal e rode install.bat de novo.
popd
endlocal
exit /b 1

:fail_python_old
echo.
echo [ERRO] A versao do Python e anterior a 3.11. Versao encontrada:
"%PYCMD%" --version
echo        Instale o 3.12:  winget install --id Python.Python.3.12 -e
popd
endlocal
exit /b 1

:fail_ffmpeg
echo.
echo [ERRO] Sem FFmpeg o CorteIA Studio nao executa a pipeline.
echo        Rode "install.bat --with-ffmpeg" ou use as instrucoes acima.
popd
endlocal
exit /b 1

:fail_venv
echo.
echo [ERRO] Falha ao criar/ativar o ambiente virtual.
echo        Teste manual:  "%PYCMD%" -m venv .venv
echo        "acesso negado"? Rode o cmd como administrador ou libere a pasta
echo        no antivirus.
popd
endlocal
exit /b 1

:fail_pip
echo.
echo [ERRO] O pip falhou ao instalar as dependencias.
echo        Causas comuns: proxy corporativo, antivirus bloqueando python.exe,
echo        conexao instavel. Repita com detalhe:
echo            .venv\Scripts\python.exe -m pip install -r requirements.txt -vv
popd
endlocal
exit /b 1

:fail_import
echo.
echo [ERRO] Bibliotecas faltando no .venv - a instalacao nao terminou.
echo        Rode de novo:  .venv\Scripts\python.exe -m pip install -r requirements.txt
popd
endlocal
exit /b 1
