; Inno Setup script for incant.
;
; Build the PyInstaller bundle first:
;   pyinstaller --name incant --onedir --windowed --icon assets/incant.ico ^
;     --add-data "assets;assets" --collect-all customtkinter ^
;     --collect-all faster_whisper --collect-all ctranslate2 ^
;     --collect-all tokenizers --noconfirm ui.py
;
; Then compile this script with Inno Setup (ISCC installer.iss).
; Output goes to dist_installer\incant-setup.exe

#define MyAppName "incant"
#define MyAppVersion "0.2.0"
#define MyAppExeName "incant.exe"

[Setup]
AppId={{8C8E6E6B-6E0E-4E0E-9C7E-9D8C7E6E6E6B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist_installer
OutputBaseFilename=incant-setup
SetupIconFile=assets\incant.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Components]
; CUDA cuBLAS DLLs (~770MB). Without this, the app falls back to CPU automatically.
Name: "app"; Description: "incant application"; Types: full compact custom; Flags: fixed
Name: "gpu"; Description: "GPU acceleration (NVIDIA CUDA, +770MB)"; Types: full

[Types]
Name: "full"; Description: "Full installation (with GPU support)"
Name: "compact"; Description: "Compact installation (CPU only)"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Files]
Source: "dist\incant\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion; Components: app
Source: ".venv\Lib\site-packages\nvidia\cublas\bin\cublas64_12.dll"; DestDir: "{app}\_internal\ctranslate2"; Components: gpu
Source: ".venv\Lib\site-packages\nvidia\cublas\bin\cublasLt64_12.dll"; DestDir: "{app}\_internal\ctranslate2"; Components: gpu

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
function HasNvidiaGpu(): Boolean;
var
  ResultCode: Integer;
begin
  // wmic exits 0 and prints a GPU name line if an NVIDIA adapter is present.
  Result := Exec(ExpandConstant('{cmd}'), '/C wmic path win32_VideoController get name | findstr /I "NVIDIA" >nul',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure InitializeWizard();
begin
  if not HasNvidiaGpu() then
    WizardForm.TypesCombo.ItemIndex := 1; // default to "compact" (CPU only)
end;
