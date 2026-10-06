; Inno Setup script for Rflow. build_installer.cmd compiles it after PyInstaller has produced dist\sst\
; (the app, without a speech model: Rflow downloads Parakeet when the user chooses it).
#ifndef AppVersion
  #error Pass /DAppVersion=x.y.z (build_installer.cmd does this)
#endif
#define AppName "Rflow"
; The tray app; rflow-cli.exe next to it is the command-line tool.
#define AppExe "Rflow.exe"
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"

[Setup]
; The same AppId as the versions called "SST Dictation", so they are upgraded in place (same folder, one entry in
; Settings > Apps).
AppId={{8F1C5A7E-3B2D-4E6A-9C1F-5D2E7A4B9C31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=karthi-ai-engineer
AppPublisherURL=https://github.com/karthi-ai-engineer/Rach_flow
AppSupportURL=https://github.com/karthi-ai-engineer/Rach_flow/issues
AppUpdatesURL=https://github.com/karthi-ai-engineer/Rach_flow/releases
; Per-user install into %LOCALAPPDATA%\Programs: no administrator rights needed.
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Rflow holds this mutex while running, so setup and uninstall ask to close it first. (An in-app update releases it
; before starting setup.) The name is from the first versions.
AppMutex=SST-Dictation-running
OutputDir=..\dist
OutputBaseFilename=Rflow-Setup-{#AppVersion}
SetupIconFile=..\sst\static\sst.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
; Rflow-branded pictures (scripts/make_installer_images.py); Inno Setup picks the size that fits the display scaling.
WizardImageFile=images\wizard-164.bmp,images\wizard-205.bmp,images\wizard-246.bmp,images\wizard-328.bmp
WizardSmallImageFile=images\wizard-small-55.bmp,images\wizard-small-69.bmp,images\wizard-small-83.bmp,images\wizard-small-110.bmp
DisableWelcomePage=no
; Python, Qt and the speech runtimes: many files, which solid LZMA2 packs best.
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=4

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nRflow types what you say, in any app: hold Ctrl+Win, speak, and let go. It recognises your speech on this computer with NVIDIA Parakeet, downloaded once when you choose it (about 660 MB), or with a cloud model or your own server.%n%nIt needs no administrator rights.
FinishedLabel=[name] is installed.%n%nWhen you click Finish, Rflow opens and helps you choose how it recognises your speech, choose your microphone and try your first dictation. After that, hold Ctrl+Win in any app and speak.

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "startup"; Description: "Start Rflow when I sign in to Windows"; GroupDescription: "Startup:"

[InstallDelete]
; Updating: remove the previous version's program files first, so no stale DLLs are left behind.
Type: filesandordirs; Name: "{app}\_internal"
; Leftovers from the versions called "SST Dictation": their programs and shortcuts.
Type: files; Name: "{app}\SST Dictation.exe"
Type: files; Name: "{app}\sst.exe"
Type: files; Name: "{autoprograms}\SST Dictation.lnk"
Type: files; Name: "{autodesktop}\SST Dictation.lnk"
Type: files; Name: "{userstartup}\SST Dictation.lnk"

[Files]
Source: "..\dist\sst\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Hold Ctrl+Win in any app and speak"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Hold Ctrl+Win in any app and speak"; Tasks: desktopicon

[Registry]
; The same value the app's "Start when I sign in" setting turns on and off (sst/settings.py), so the two always agree.
Root: HKCU; Subkey: "{#RunKey}"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"" --startup"; Tasks: startup
; Remove it on uninstall even when it was switched on later from the app's settings.
Root: HKCU; Subkey: "{#RunKey}"; ValueType: none; ValueName: "{#AppName}"; Flags: uninsdeletevalue
; The startup entry of the versions called "SST Dictation".
Root: HKCU; Subkey: "{#RunKey}"; ValueType: none; ValueName: "SST Dictation"; Flags: deletevalue uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent
; An in-app update runs setup with /SILENT /UPDATE=1: start the new version when it's done.
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: IsInAppUpdate

[Code]
function IsInAppUpdate: Boolean;
begin
  Result := ExpandConstant('{param:UPDATE|0}') = '1';
end;
