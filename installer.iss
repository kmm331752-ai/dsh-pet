; DSH 桌宠的安装包脚本（Inno Setup 6）
;
; 用法：
;   1. 先跑 python build_exe.py 生成 dist\DSH-Pet.exe
;   2. 用 Inno Setup 编译本文件：
;        iscc installer.iss
;      或者打开 Inno Setup 的 IDE 直接 F9
;
; 产物在 installer\DSH-Pet-Setup-<版本>.exe
;
; 设计取舍：只装一个 exe + 开始菜单/桌面快捷方式 + 卸载项。
; 不注册开机自启 —— 那是应用自己的设置项（设置窗口里那个开关），
; 安装时替你勾上会在卸载后留下一条指向空路径的 Run 项。

#define AppName "DSH 桌宠"
#define AppExe "DSH-Pet.exe"
#define AppVersion "1.15.0"
#define AppPublisher "kmm331752-ai"
#define AppUrl "https://github.com/kmm331752-ai/dsh-pet"

[Setup]
AppId={{8F3A21C4-7B6E-4D92-9A15-3C0E7F2B8D41}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=DSH-Pet-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 只有 64 位 Windows 有分层窗口 API 的完整支持
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 关掉"卸载时问要不要保留配置"那一堆 —— 配置就在安装目录旁边，
; 卸载会一起删掉，README 里写了怎么手动备份

[Languages]
; Inno Setup 默认只带 Default.isl（英文）。想加中文要自己把
; Languages\ChineseSimplified.isl 放进 Inno 的安装目录（官方仓库有非官方译本）。
; 之前这里写了两条都指向 Default.isl —— 那条 "chinese" 其实显示的是英文，
; 只是名字叫 chinese 而已。属于自欺欺人，删掉。
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
Name: "startup"; Description: "开机自动启动"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "assets\ATTRIBUTION.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; 只有用户勾了才写；卸载时 Inno 会自动清掉（uninsdeletevalue）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "DSH-Pet"; ValueData: """{app}\{#AppExe}"""; \
    Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即启动 {#AppName}"; \
    Flags: nowait postinstall skipifsilent
