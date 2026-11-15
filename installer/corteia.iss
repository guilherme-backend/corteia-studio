; ===========================================================================
;  CorteIA Studio - roteiro do Inno Setup 6
;
;  Compilar:  build_windows.bat                 (chama iscc installer\corteia.iss)
;  Manual:    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\corteia.iss
;
;  Preco: o PyInstaller precisa ter rodado antes, gerando "dist\CorteIA Studio\"
;  (one-dir). Este arquivo nao congela nada sozinho.
;
;  NAO COMPILADO no ambiente de referencia desta entrega (Linux: sem Windows,
;  sem iscc, sem PyInstaller). A sintaxe segue a documentacao do Inno Setup 6 e
;  precisa ser validada numa maquina Windows real; o roteiro de validacao esta
;  em docs\WINDOWS.md.
; ===========================================================================

#define MyAppName      "CorteIA Studio"
#define MyAppVersion   "1.0.0"
#define MyAppPublisher "CorteIA Studio"
#define MyAppURL       "https://github.com/corteia-studio/corteia-studio"
#define MyAppExeName   "CorteIA Studio.exe"
#define MyBuildDir     "dist\CorteIA Studio"

[Setup]
; AppId fixo. A mesma GUID em toda versao faz o Windows tratar os instaladores
; como o MESMO programa: atualiza por cima em vez de criar dois itens no Painel
; de Controle. Gerada com uuid5(NAMESPACE_URL, "<projeto>/installer"); depois da
; primeira publicacao NUNCA mais troque esta linha.
AppId={{C235B928-167F-5802-88ED-DF018813DBDC}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

; {autopf} = "Program Files" na instalacao por maquina, e LocalAppData se o
; usuario escolher "apenas para mim" (PrivilegesRequiredOverridesAllowed).
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

; Saida: dist\CorteIA-Setup-1.0.0.exe
OutputDir=dist
OutputBaseFilename=CorteIA-Setup-{#MyAppVersion}
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

; 64 bits em todo o bundle (PySide6, ctranslate2, FFmpeg).
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

; .exe nao assinado: o SmartScreen vai avisar na primeira execucao. Assinatura
; de codigo exige certificado pago e nao existe nesta entrega (README ->
; Limitacoes; docs\WINDOWS.md -> SmartScreen).
SignedUninstaller=no

Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
MinVersion=6.1sp1
CloseApplications=yes
RestartApplications=no

; Este arquivo fica em installer\, mas todos os caminhos abaixo (dist\,
; assets\, OutputDir=dist) sao relativos a RAIZ do projeto. Sem SourceDir o
; Inno procuraria installer\dist\ e gravaria o instalador em installer\dist\.
SourceDir=..

[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\Portuguese.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"

[Components]
; O programa em si, nao desmarcavel.
Name: "main"; Description: "{#MyAppName} (programa)"; Flags: fixed; Types: full
; Os pesos do Whisper NAO vao no instalador: 3 GB de weights nao combinam com
; um download de 400 MB, e o app baixa o modelo sozinho na primeira execucao.
; Estas linhas so fazem sentido se quem compilou colocar o modelo pre-baixado
; em _models\ ao lado deste arquivo (skipifsourcedoesntexist abaixo).
Name: "model_tiny"; Description: "Modelo Whisper tiny (78 MB)"; Types: custom full
Name: "model_base"; Description: "Modelo Whisper base (148 MB, padrao)"; Types: custom full
Name: "model_small"; Description: "Modelo Whisper small (486 MB)"; Types: custom

[Files]
; O bundle congelado inteiro, preservando a arvore de dist\CorteIA Studio\.
Source: "{#MyBuildDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; O icone usado pelo atalho, fora do bundle para o caminho ficar curto.
Source: "assets\icon.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
; Modelos opcionais pre-baixados (vao para a pasta de dados do usuario, que e
; onde corteia/paths.py models_dir() procura). Coloque o modelo em
; installer\_models\<nome>\ antes de compilar; sem a pasta, a linha e ignorada.
Source: "installer\_models\*"; DestDir: "{userappdata}\CorteIA Studio\models"; \
    Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist \
    Components: model_tiny model_base model_small

[Dirs]
; Pastas de dados/estado. O app tambem as cria sozinho ao iniciar; cria-las
; aqui garante a permissao correta desde o inicio para instalacoes por maquina.
Name: "{userappdata}\CorteIA Studio"; Flags: uninsneveruninstall
Name: "{userappdata}\CorteIA Studio\projects"; Flags: uninsneveruninstall
Name: "{userappdata}\CorteIA Studio\models"; Flags: uninsneveruninstall
Name: "{userappdata}\CorteIA Studio\logs"; Flags: uninsneveruninstall
Name: "{localappdata}\CorteIA Studio\Cache"; Flags: uninsneveruninstall
Name: "{localappdata}\CorteIA Studio\Temp"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"
Name: "{group}\{cm:ProgramOnTheWeb,{#MyAppName}}"; Filename: "{#MyAppURL}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon

[Run]
; A caixinha "executar ao terminar" e nao-modal e pulada em modo silencioso.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; \
    WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
; Diagnostico de ambiente sem abrir a interface: FFmpeg, codecs, libass, fontes,
; modelos e caminhos; sai com 0 quando esta tudo pronto.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--check"; \
    Description: "Rodar o diagnostico (--check) e ver o resultado"; \
    WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\CorteIA Studio\Temp"

[Code]
; ---------------------------------------------------------------------------
;  Pre-requisitos: FFmpeg e Visual C++ 2015-2022 x64 Redistributable
; ---------------------------------------------------------------------------
; Sem FFmpeg o programa abre, mas nada analisa nem exporta: ele faz probe,
; corte, codificacao e a queimadura da legenda (libass). Em vez de instalar o
; FFmpeg por cima -- decisao que nao e nossa -- o instalador detecta e orienta,
; em portugues.

const
  FFMSG = 'O CorteIA Studio precisa do FFmpeg, e ele nao foi encontrado neste computador.'#13#10#13#10 +
          'O que fazer (escolha uma):'#13#10 +
          '  1) Num terminal (cmd ou PowerShell):'#13#10 +
          '       winget install --id Gyan.FFmpeg -e'#13#10 +
          '     Depois FECHE e REABRA o terminal e o CorteIA Studio: o PATH novo'#13#10 +
          '     so vale para sessoes novas.'#13#10 +
          '  2) Baixe "ffmpeg-master-latest-win64-gpl.zip" em'#13#10 +
          '       https://www.gyan.dev/ffmpeg/builds/'#13#10 +
          '     descompacte em C:\ffmpeg e adicione C:\ffmpeg\bin ao Path do sistema.'#13#10 +
          '  3) Ou copie ffmpeg.exe e ffprobe.exe para dentro da pasta'#13#10 +
          '       C:\Program Files\CorteIA Studio\bin\'#13#10 +
          '     (o app procura o FFmpeg na propria pasta e em bin\ antes de olhar o PATH).'#13#10#13#10 +
          'O build precisa ter libx264 e libass. O build "essentials" as vezes vem sem libx265:'#13#10 +
          'isso nao bloqueia nada, exporte em H.264.';

  VCMSG = 'As bibliotecas do programa (Qt e CTranslate2) precisam do Visual C++ 2015-2022 Redistributable x64.'#13#10#13#10 +
          'Se o CorteIA Studio nao abrir, instale-o em:'#13#10 +
          '  https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist'#13#10#13#10 +
          'Maquinas com Office, Chrome ou Steam normalmente ja tem.';

function FfmpegOnPath: Boolean;
var
  PathStr, Dir, Candidate: string;
  P: Integer;
begin
  Result := False;
  PathStr := GetEnv('PATH');
  if PathStr = '' then Exit;
  repeat
    P := Pos(';', PathStr);
    if P > 0 then
    begin
      Dir := Copy(PathStr, 1, P - 1);
      Delete(PathStr, 1, P);
    end
    else
    begin
      Dir := PathStr;
      PathStr := '';
    end;
    Dir := Trim(Dir);
    if Dir = '' then Continue;
    Candidate := Dir + '\ffmpeg.exe';
    if FileExists(Candidate) then
    begin
      Log('FFmpeg encontrado em: ' + Candidate);
      Result := True;
      Exit;
    end;
  until PathStr = '';
end;

function VcRedistPresent: Boolean;
var
  Installed: Cardinal;
begin
  Result := False;
  // A chave "...\14.0\VC\Runtimes\X64" cobre toda a serie 2015-2022.
  if RegQueryDWordValue(HKLM64,
       'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64',
       'Installed', Installed) and (Installed = 1) then
    Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then Exit;

  if FfmpegOnPath then
    Log('FFmpeg: ok no PATH')
  else if FileExists(ExpandConstant('{app}\bin\ffmpeg.exe')) then
    Log('FFmpeg: encontrado dentro do bundle')
  else
    MsgBox(FFMSG, mbInformation, OK);

  if not VcRedistPresent then
    MsgBox(VCMSG, mbInformation, OK)
  else
    Log('VC++ redistributable: ok');
end;

; Impede instalar uma versao antiga por cima de uma mais nova.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  CurVersion: string;
begin
  Result := '';
  NeedsRestart := False;
  if RegQueryStringValue(HKLM64,
       'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{' +
       'C235B928-167F-5802-88ED-DF018813DBDC}_is1',
       'DisplayVersion', CurVersion) then
  begin
    Log('Versao instalada detectada: ' + CurVersion);
    if CurVersion > '{#MyAppVersion}' then
      Result := 'Existe uma versao mais nova do {#MyAppName} instalada (' +
                CurVersion + '). Desinstale-a antes de instalar esta.';
  end;
end;
