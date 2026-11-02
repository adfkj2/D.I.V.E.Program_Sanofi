; Inno Setup 6 script. Per-user install: no administrator rights required.
; User data lives in {localappdata}\WeOnceTalked\data and is deliberately never
; touched by the uninstaller, so reinstalling keeps every conversation.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#define RepoRoot AddBackslash(AddBackslash(SourcePath) + "..\..")
#define SourceDir RepoRoot + "dist\WeOnceTalked"
#define OutputDir RepoRoot + "dist\windows"

[Setup]
AppId={{7B1C4F60-9D2A-4E7B-A0C1-5E6F3A2B8D41}
AppName=我们曾聊过
AppVersion={#AppVersion}
AppVerName=我们曾聊过 {#AppVersion}
AppPublisher=WeOnceTalked
AppComments=本地运行的对话与记忆应用
DefaultDirName={localappdata}\Programs\WeOnceTalked
DefaultGroupName=我们曾聊过
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename=WeOnceTalked-{#AppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
SetupIconFile=we-once-talked.ico
UninstallDisplayIcon={app}\WeOnceTalked.exe
UninstallDisplayName=我们曾聊过
AllowNoIcons=yes

[Languages]
; Inno Setup 6 ships no Simplified Chinese translation by default; official builds
; fall back to English so the installer never fails to compile.
#if FileExists(AddBackslash(CompilerPath) + "Languages\ChineseSimplified.isl")
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#else
Name: "english"; MessagesFile: "compiler:Default.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\我们曾聊过"; Filename: "{app}\WeOnceTalked.exe"; WorkingDir: "{app}"
Name: "{group}\卸载 我们曾聊过"; Filename: "{uninstallexe}"
Name: "{autodesktop}\我们曾聊过"; Filename: "{app}\WeOnceTalked.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\WeOnceTalked.exe"; Description: "立即启动 我们曾聊过"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only the program directory. {localappdata}\WeOnceTalked\data is user data.
Type: filesandordirs; Name: "{app}"
