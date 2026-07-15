; 获客虾独立安装器。用户数据保存在 %LOCALAPPDATA%\LeadShrimp\data，升级与卸载默认保留。
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef StagingDir
  #define StagingDir "..\..\staging\LeadShrimp"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\release"
#endif

[Setup]
AppId={{41C0A405-8A77-42AF-94A9-4E3A036975EB}
AppName=获客虾
AppVersion={#AppVersion}
AppPublisher=问舟
DefaultDirName={autopf}\LeadShrimp
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir={#OutputDir}
OutputBaseFilename=LeadShrimpSetup_{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
CloseApplicationsFilter=LeadShrimpLauncher.exe
UninstallDisplayName=获客虾
UninstallDisplayIcon={app}\LeadShrimpLauncher.exe

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式:"

[Files]
Source: "{#StagingDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\获客虾"; Filename: "{app}\LeadShrimpLauncher.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\获客虾"; Filename: "{app}\LeadShrimpLauncher.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\LeadShrimpLauncher.exe"; Description: "立即启动获客虾"; Flags: nowait postinstall skipifsilent

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\assets"
Type: files; Name: "{app}\LeadShrimpLauncher.exe"

[Code]
function DataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\LeadShrimp\data');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if DirExists(DataDir()) then
      if MsgBox('是否同时删除获客虾用户数据？' + #13#10 +
        '包含 Cookie、线索和导出文件。默认【否】保留数据。', mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir(), True, True, True);
  end;
end;
