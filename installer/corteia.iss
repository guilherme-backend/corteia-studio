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
; Os pesos do Whisper nao vao no instalador: o app baixa o modelo escolhido
; pelo usuario sozinho na primeira execucao.

[Files]
; O bundle congelado inteiro, preservando a arvore de dist\CorteIA Studio\.
Source: "{#MyBuildDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; O icone usado pelo atalho, fora do bundle para o caminho ficar curto.
Source: "assets\icon.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
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
// O aplicativo verifica FFmpeg, CUDA/CPU e demais pré-requisitos ao iniciar.
// O instalador permanece sem lógica adicional para manter compatibilidade com
// diferentes versões do compilador Inno Setup 6.
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
