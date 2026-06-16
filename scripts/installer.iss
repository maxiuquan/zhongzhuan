; Inno Setup Script for Zhongzhuan
#define MyAppName "Zhongzhuan"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Zhongzhuan"
#define MyAppURL "https://github.com/zhongzhuan/zhongzhuan"
#define MyAppExeName "zhongzhuan.exe"

[Setup]
AppId={{B8A1C3D5-E6F7-4A8B-9C0D-1E2F3A4B5C6D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=Zhongzhuan-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin

[Languages]
Name: "chinese"; MessagesFile: "compiler:Languages\Chinese.isl"

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config.yaml"; DestDir: "{commonappdata}\Zhongzhuan"; Flags: ignoreversion onlyifdoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--install"; StatusMsg: "正在注册 Windows 服务..."; Flags: runhidden
Filename: "{app}\{#MyAppExeName}"; Parameters: "--start"; StatusMsg: "正在启动服务..."; Flags: runhidden
Filename: "http://127.0.0.1:8089"; Description: "打开管理后台"; Flags: postinstall shellexec nowait

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--stop"; Flags: runhidden
Filename: "{app}\{#MyAppExeName}"; Parameters: "--uninstall"; Flags: runhidden