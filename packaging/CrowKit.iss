#ifndef AppVersion
  #define AppVersion "0.1.4"
#endif
#ifndef StageDir
  #define StageDir "..\build\stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif

#define AppName "CrowKit"
#define AppPublisher "CrowKit"
#define AppExeName "CrowKit.exe"
#define AppIcon "..\app\web\assets\crowkit.ico"

[Setup]
AppId={{A78B1722-CC53-4E32-8335-31609DF76360}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir={#OutputDir}
OutputBaseFilename=CrowKit-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LanguageDetectionMethod=uilanguage
ShowLanguageDialog=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.22000
UninstallDisplayIcon={app}\app\web\assets\crowkit.ico
SetupIconFile={#AppIcon}
LicenseFile={#SourcePath}\DISCLAIMER_ZH.txt
SetupLogging=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "{#SourcePath}\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式:"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\小鸦工具箱"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\main.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\web\assets\crowkit.ico"
Name: "{group}\卸载小鸦工具箱"; Filename: "{uninstallexe}"; IconFilename: "{app}\app\web\assets\crowkit.ico"
Name: "{autodesktop}\小鸦工具箱"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\main.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\web\assets\crowkit.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\app\main.py"""; WorkingDir: "{app}"; Description: "启动小鸦工具箱"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\reg.exe"; Parameters: "DELETE ""HKCU\Software\Microsoft\Windows\CurrentVersion\Run"" /v ""CrowKit-CampusNetworkAuth"" /f"; RunOnceId: "RemoveCampusNetworkAutostart"; Flags: runhidden

[Code]
function EdgeInstalled(): Boolean;
begin
  Result :=
    FileExists(ExpandConstant('{pf32}\Microsoft\Edge\Application\msedge.exe')) or
    FileExists(ExpandConstant('{pf}\Microsoft\Edge\Application\msedge.exe')) or
    FileExists(ExpandConstant('{localappdata}\Microsoft\Edge\Application\msedge.exe'));
end;

function InitializeSetup(): Boolean;
begin
  Result := EdgeInstalled();
  if not Result then
    MsgBox('CrowKit 需要 Microsoft Edge。请先安装或修复 Windows 11 自带的 Edge。', mbError, MB_OK);
end;
