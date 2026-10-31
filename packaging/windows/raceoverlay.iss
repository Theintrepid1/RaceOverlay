; SPDX-License-Identifier: GPL-3.0-or-later
; Inno Setup script for the Windows installer. Built by build_windows.py,
; which passes AppVersion, AppDir (the PyInstaller folder), IconFile and
; LicenseFile with /D. Per-user install: no admin prompt.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Fixed GUID: lets later installers upgrade this one in place.
AppId={{6B0E3D52-9A4C-4C1E-9E59-3A1F2C7D5B41}
AppName=Race Overlay
AppVersion={#AppVersion}
AppVerName=Race Overlay {#AppVersion}
AppPublisher=Theintrepid1
AppPublisherURL=https://github.com/Theintrepid1/RaceOverlay
DefaultDirName={localappdata}\Programs\Race Overlay
DefaultGroupName=Race Overlay
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile={#LicenseFile}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\RaceOverlay.exe
OutputBaseFilename=RaceOverlay-{#AppVersion}-windows-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Desktop shortcuts"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Clear the old program files on upgrade so removed libraries don't linger.
; (Settings, logs and tracks live elsewhere and are untouched.)
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
; Each app has its own AppUserModelID (set again at startup, osutil.set_app_id),
; so the two get separate taskbar buttons with their own icons.
Name: "{group}\Race Overlay"; Filename: "{app}\RaceOverlay.exe"; Comment: "Sync telemetry to video and render gauge overlays"; AppUserModelID: "Theintrepid1.RaceOverlay"
Name: "{group}\Race Analysis"; Filename: "{app}\RaceOverlay.exe"; Parameters: "--analysis"; IconFilename: "{app}\raceanalysis.ico"; Comment: "Compare laps and sessions"; AppUserModelID: "Theintrepid1.RaceAnalysis"
Name: "{group}\Uninstall Race Overlay"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Race Overlay"; Filename: "{app}\RaceOverlay.exe"; Tasks: desktopicon; AppUserModelID: "Theintrepid1.RaceOverlay"
Name: "{autodesktop}\Race Analysis"; Filename: "{app}\RaceOverlay.exe"; Parameters: "--analysis"; IconFilename: "{app}\raceanalysis.ico"; Tasks: desktopicon; AppUserModelID: "Theintrepid1.RaceAnalysis"

[Run]
Filename: "{app}\RaceOverlay.exe"; Parameters: "--analysis"; Description: "Start Race Analysis"; Flags: nowait postinstall skipifsilent unchecked
Filename: "{app}\RaceOverlay.exe"; Description: "Start Race Overlay"; Flags: nowait postinstall skipifsilent
