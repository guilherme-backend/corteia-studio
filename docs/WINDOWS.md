# Compilar e verificar o instalador Windows

**Nada deste roteiro foi executado no ambiente onde o CorteIA Studio foi
escrito.** A máquina de referência é Linux, sem Windows, sem PyInstaller e sem
Inno Setup: `corteia.spec`, `build_windows.bat` e `installer/corteia.iss` estão
**escritos e revisados, não compilados**. Esta página é a ordem exata para
validar numa máquina Windows real — e essa validação ainda não foi feita.

## 1. Preparar a máquina de build (Windows 10/11 x64)

```bat
winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
winget install --id JRSoftware.InnoSetup -e --accept-source-agreements --accept-package-agreements
```

Feche e reabra o `cmd` depois de cada instalação: o `PATH` novo só vale para
sessões novas. Confira os três:

```bat
python --version          :: 3.12.x
where ffmpeg              :: ...\WinGet\...\ffmpeg.exe
where iscc                :: C:\Program Files (x86)\Inno Setup 6\ISCC.exe
```

Se `where iscc` não responder, `build_windows.bat` ainda procura o Inno Setup
pelos caminhos padrão (`Program Files` e `Program Files (x86)`). Se não achar,
ele para com a mensagem em português apontando
<https://jrsoftware.org/isdl.php> e sugere `build_windows.bat --portable`, que
gera só o bundle.

## 2. Congelar e compilar o instalador

```bat
cd corteia-studio
build_windows.bat
```

O script, nessa ordem: cria/reaproveita `.venv`; exige Python ≥ 3.11; instala
`requirements-build.txt` (que puxa `requirements.txt` + `pyinstaller>=6.11,<7`
+ `pyinstaller-hooks-contrib`); roda

```bat
python -m PyInstaller --noconfirm --log-level WARN corteia.spec
iscc installer\corteia.iss
```

Saída esperada:

```
dist\CorteIA Studio\           :: bundle one-dir, com "CorteIA Studio.exe"
dist\CorteIA-Setup-1.0.0.exe   :: instalador
```

Etapas manuais, para controlar uma a uma:

```bat
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-build.txt
pyinstaller --noconfirm corteia.spec
"dist\CorteIA Studio\CorteIA Studio.exe" --check     :: o BUNDLE, antes de empacotar
"dist\CorteIA Studio\CorteIA Studio.exe" --smoke
iscc installer\corteia.iss
```

Passar no `--check` e no `--smoke` **do bundle** antes de rodar o `iscc` é o
que separa “o PyInstaller esqueceu um módulo” de “o app tem um bug”. Dois
sinais típicos de spec incompleto: `ModuleNotFoundError: faster_whisper` na
hora de analisar, e “o player não abre” (`PySide6.QtMultimedia` fora do
bundle).

### O que o spec faz e por quê

- **one-dir**, não one-file: CTranslate2, OpenCV e Qt carregam `.dll` em tempo
  de execução; one-file extrai tudo para `%TEMP%` a cada abertura, inicializa
  devagar e irrita antivírus.
- `datas`: `corteia/resources/qss`, `models` (o YuNet ONNX) e `i18n`. A pasta
  `icons` **não existe** no projeto, e o spec pula o que não está no disco em
  vez de quebrar o build — os ícones da interface são desenhados com `QPainter`
  em `corteia/ui/theme.py`.
- `hiddenimports` do que é importado **dentro de função**, que o analizador não
  vê: `faster_whisper` (`core/transcribe.py:540`), `ctranslate2`
  (`hardware.py:194`), `yt_dlp` + `yt_dlp.extractor.*` (`core/downloader.py:169`),
  `scenedetect.*` (`core/scenes.py:28/78`), `onnxruntime`, `psutil`, `tqdm`,
  `cv2`, `numpy`, `PIL`, `huggingface_hub`, `tokenizers`, `av`,
  `PySide6.QtMultimedia`/`QtMultimediaWidgets` (`ui/widgets/player.py:29/36`),
  `shiboken6`, `winreg` (Windows, `core/fonts.py:85`) e as seis páginas da UI.
- `excludes`: `tkinter`, `pydoc_data.topics`, `torch`/`torchaudio` (o app nunca
  importa torch; `hardware.py:202` tenta e trata `ImportError` — excluir
  economiza ~2,5 GB), `numpy.tests`, `unittest.tests`, `pytest`, `IPython`,
  `matplotlib`, `setuptools`/`pkg_resources` e os módulos Qt de
  WebEngine/QML/3D/Charts.
- `console=False`, ícone `assets/icon.ico`, `upx=False` (UPX quebra assinatura e
  é flaggado por antivírus).
- No `.iss`, `SourceDir=..`: o script mora em `installer\`, mas os caminhos
  `dist\CorteIA Studio\*` e `assets\icon.ico` e a saída `dist\` são relativos à
  raiz do projeto. Sem isso, o Inno procuraria `installer\dist\`.

### Modelo Whisper no instalador

Os pesos **não** vão dentro do `.exe` (3,1 GB do `large-v3` não combinam com um
download de ~400 MB). O app baixa na primeira execução em
`%APPDATA%\CorteIA Studio\models\`. Para um instalador offline, coloque o modelo
pre-baixado em `installer\_models\<nome>\` antes de compilar: os componentes
`model_tiny`/`model_base`/`model_small` copiam aquela pasta para a pasta de
modelos, e `skipifsourcedoesntexist` faz a linha ser ignorada quando não há
nada lá.

## 3. Checklist numa máquina limpa

Máquina limpa = Windows recém-instalado, sem Python, sem FFmpeg, sem Qt.
Idealmente uma VM com snapshot.

- [ ] **Duplo clique** em `CorteIA-Setup-1.0.0.exe`. O SmartScreen aparece?
      Transcreva o texto exato da tela e o nome do arquivo.
- [ ] Testar **“Apenas para mim”** (sem admin) **e** “Para todos os usuários”
      (com admin): `PrivilegesRequiredOverridesAllowed=dialog` precisa funcionar
      nos dois, inclusive o `{autopf}` resolvendo para `LocalAppData` no
      primeiro caso.
- [ ] Desmarcar “ícone na área de trabalho” e confirmar que o atalho do menu
      Iniciar continua e abre.
- [ ] Abrir pelo atalho. **Nada de janela preta de console** (`console=False`),
      e o ícone do CorteIA na barra de tarefas. Ícone genérico de Python =
      `SetupIconFile`/`icon=` falhou.
- [ ] O aviso de **FFmpeg ausente** saiu em português, com os três caminhos
      (winget, gyan.dev, copiar para `{app}\bin`)? Ele vem de
      `[Code] CurStepChanged(ssPostInstall)`.
- [ ] O aviso de **VC++ redistributable** apareceu se a chave
      `HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64\Installed`
      não existir? Numa máquina com Office/Chrome/Steam normalmente não
      aparece — confirme lendo a chave, não por suposição.
- [ ] Rodar `--check` pelo atalho “Rodar o diagnóstico (--check)” do menu
      Iniciar: deve sair 0 e imprimir codecs, libass, fontes, modelos e
      caminhos.
- [ ] Instalar o FFmpeg pelo winget, **fechar e reabrir** o app, confirmar que
      o `--check` agora acha o FFmpeg.
- [ ] Importar um `.mp4` real, deixar a análise terminar, exportar 1 corte em
      720p e abrir o MP4 no Visualizador de Filmes do Windows (verificar que a
      legenda está queimada e o recorte segue o rosto).
- [ ] Testar caminho com **espaço, acento e ç**:
      `C:\Users\José\Videos\vídeo final.mp4`, e um título de corte com emoji.
      A sanitização é `corteia/context.py sanitize_name()`; o libass no Windows
      tem histórico de reclamar de caminho não-ASCII em filtergraph — se a
      legenda sumir *só* nesta máquina, procure aí.
- [ ] Reabrir o projeto (`Ctrl+O`), mexer em um corte, fechar, abrir de novo.
- [ ] **Atualização:** instalar a 1.0.1 por cima da 1.0.0 e conferir que o
      Painel de Controle mostra **um** item, não dois. É o `AppId` fixo
      (`{{C235B928-167F-5802-88ED-DF018813DBDC}`) que garante isso — nunca o
      troque depois da primeira publicação.
- [ ] **Desinstalar** pelo Painel de Controle: o programa sai e
      `%APPDATA%\CorteIA Studio` (config, projetos, modelos, log) **continua**
      — decisão deliberada, nada de apagar trabalho do usuário. Só
      `%LOCALAPPDATA%\CorteIA Studio\Temp` some (`[UninstallDelete]`).

## 4. SmartScreen e assinatura de código

O instalador **não é assinado**. Sem certificado, o Windows mostra na primeira
execução:

> “O Windows protegeu seu computador — Aplicativo não confiável publicado por
> ‘Nenhum editor’.”

Caminho honesto: **Mais informações → Executar assim mesmo**. Não existe opção
de build que resolva isso. Para eliminar o aviso é preciso comprar um
certificado (OV ou EV), assinar o executável **depois** do `iscc` e acumular
reputação de download:

```bat
signtool.exe sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a ^
    dist\CorteIA-Setup-1.0.0.exe
```

Se um dia assinar, mude também `SignedUninstaller=yes` no `.iss` (Inno assina o
`unins000.exe` durante a instalação quando você configura
`SigningOptions`/`DigSigSigner`).

## 5. Visual C++ redistributable

As DLLs do Qt 6 e do CTranslate2 ligam contra `VCRUNTIME140.dll`,
`VCRUNTIME140_1.dll` e `MSVCP140.dll`. Se eles faltarem, o sintoma **não** é uma
mensagem “falta o Visual C++”: o processo nem abre, ou aparece a caixa
“The code execution cannot proceed because API-MS-WIN-CRT-RUNTIME-... .DLL was
not found”. É o motivo da checagem no instalador e do link na mensagem:

<https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist>

Alternativa mais gorda (~15 MB no bundle) e à prova de máquina limpa: copiar as
três DLLs acima para dentro de `dist\CorteIA Studio\` na máquina de build — elas
entram no instalador pelo `Source: "{#MyBuildDir}\*"` e dispensam o
redistributável no destino.

## 6. Registro do teste

Quando este roteiro for executado de verdade, atualize: a tabela de
*Limitações* do `README.md` (linhas sobre Windows e assinatura), o bloco “o que
ainda não é testável aqui” de `docs/TESTES.md` e o `[Unreleased]` do
`CHANGELOG.md`, anotando a versão exata do Inno Setup, do PyInstaller e da build
do Windows usada.

## 7. O que já foi provado neste ambiente e o que só pode ser provado em Windows

Ambiente da auditoria: Linux x86_64, Python 3.12.3, PyInstaller 6.22.3,
`pyinstaller-hooks-contrib` instalado. Data: 2026-11-14.

**Provado aqui (executado de fato):**

a) **`pyinstaller corteia.spec` rodou em Linux x86_64 e terminou com EXIT=0**,
   produzindo `dist/CorteIA Studio/` (731 arquivos, 1214,7 MiB). O comando
   usado foi `.venv/bin/python -m PyInstaller --noconfirm --log-level WARN
   corteia.spec`. O spec reescrito foi executado, não só revisado: o log
   verboso novo (`section 0..5`) imprimiu `datas`, `binaries`, as 1055
   `hiddenimports` deduplicadas e o resumo pós-`Analysis`.

b) **O binário congelado foi executado neste ambiente**, com display virtual:
   `xvfb-run -a "dist/CorteIA Studio/CorteIA Studio" --version` → `CorteIA
   Studio 1.0.0`, exit 0; e `... --check` → diagnóstico completo, exit 0,
   incluindo `FFmpeg ok`, `codec libx264/libx265/aac ok`, `legendas (libass)
   ok` e os três modelos Whisper carregando em `cpu/int8`. Ou seja: o bundle
   congelado **importa PySide6, cv2, ctranslate2, onnxruntime, av e
   faster_whisper sem DLL faltando** — no Linux. Isso não transfere para o
   Windows.

c) **NENHUM `.exe` foi gerado nem executado em Windows.** Não há Windows,
   `iscc`, `signtool` nem `nvidia-smi` aqui, e o PyInstaller não faz
   cross-compile: o spec agora avisa disso no topo do log quando
   `sys.platform != win32`.

d) **`installer/corteia.iss` nunca passou pelo Inno Setup.** A sintaxe segue a
   documentação do Inno Setup 6 e foi relida, mas `ISCC.exe` não existe neste
   ambiente; o `.iss` pode conter erro que só aparece na compilação.

e) **As DLLs específicas de Windows não podem ser verificadas aqui**:
   `qwindows.dll`, `Qt6Core/Gui/Widgets/Multimedia/Svg.dll`, `ctranslate2.dll`
   e `_ext.pyd`, `av.libs\avcodec-*.dll`/`avformat`/`avutil`/`swresample`/
   `swscale`, `onnxruntime\capi\*.dll`, `cv2\*.pyd` e as DLLs de cuDNN 9 /
   cuBLAS 12 não existem nos wheels Linux instalados. O mesmo vale para o
   `VCRUNTIME140*.dll`/`MSVCP140.dll` e para `MSVC redist` no registro.

**A checagem automática funcionando em dados reais:** `scripts/check_package.py`
foi rodado contra o pack Linux e devolveu `8/28 PRESENTES`, `13 OBRIGATÓRIOS
FALTANDO`, exit code **1** — exatamente o comportamento esperado: presentes o
executável, `corteia/resources/qss/{dark,light}.qss`,
`corteia/resources/i18n/en.json`,
`corteia/resources/models/face_detection_yunet_2023mar.onnx`, `assets/icon.png`,
`assets/icon.ico` e `base_library.zip`; faltando apenas itens `.dll`/`.pyd`,
cada um anotado com "(esperado: este bundle não é do Windows)".

Descoberta dessa execução, já refletida no verificador: **o PyInstaller 6 coloca
o `.exe` na raiz de `dist/CorteIA Studio/` e TODO o resto (DLLs, `_internal/
base_library.zip`, os `corteia/resources/`) dentro de `_internal/**`.** Ou seja,
no Windows o caminho real é `dist\CorteIA Studio\_internal\PySide6\Qt\plugins\
platforms\qwindows.dll` — não `PySide6/qtplugins/platforms/`, nem a raiz. O
`check_package.py` varre a raiz **e** `_internal/`; o `.iss` já pega os dois com
`Source: "{#MyBuildDir}\*"` recursivo, e `paths.bundle_root()` resolve via
`sys._MEIPASS`, que aponta para `_internal`.

**Só pode ser provado numa Windows 10/11 x64 real** (nada disto roda em Linux):

1. `build_windows.bat` inteiro, ou `.github/workflows/build-windows.yml`: o
   freeze produzindo `CorteIA Studio.exe` e o `check_package.py` devolvendo 0.
2. `ISCC.exe installer\corteia.iss` compilando (e o `MinVersion`,
   `ArchitecturesAllowed=x64compatible`, `PrivilegesRequiredOverridesAllowed`).
3. Instalação limpa sem Python e sem FFmpeg no PATH; avisos de FFmpeg e de
   VC++ redist; as duas variantes de privilégio (admin vs "apenas para mim").
4. Abrir a janela (é aqui que `qwindows.dll` falta ou não falta), ícone na
   barra de tarefas, ausência de console preto.
5. `--check` e `--smoke` **do .exe congelado** (com `console=False`, redirecione:
   `"dist\CorteIA Studio\CorteIA Studio.exe" --check > check.txt 2>&1`).
6. Análise de um vídeo real, exportação, legenda queimada (libass), recorte de
   rosto (YuNet), caminho com espaço/acento/emoji.
7. CUDA: com cuDNN 9 + cuBLAS 12 no PATH, `--check` deve imprimir a GPU e
   `cuda_available`; sem eles, o app cai para `cpu/int8` e mostra a nota
   "Este FFmpeg tem NVENC, mas não há runtime CUDA ativo..." — comportamento
   nunca observado aqui, pois não há GPU NVIDIA neste ambiente. Nota: a
   detecção AMD/integrada usa `wmic`, removido em builds recentes do Windows 11.
8. Persistência (trocar tema/idioma, reabrir), desinstalar e conferir resíduos,
   SmartScreen e antivírus.
