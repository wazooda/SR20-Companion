; SR20 Realism Mod installer (release plan docs/plans/2026-11-20-release-v1.md §2; installer spec
; docs/plans/companion_tray_installer.md §2). Built by tools/release/build_release.py, which passes:
;   /DAppVersion=1.0.0 /DStage=<staging folder with package\ and SR20Companion\ and docs\> /DOutDir=<output folder>
; Per-user, no admin. Never run while the sim is open.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Stage
  #define Stage "..\..\release\stage"
#endif
#ifndef OutDir
  #define OutDir "..\..\release\out"
#endif
#define PkgName "paulw-aircraft-sr20"

[Setup]
AppId={{7B0E2F4A-5C1D-4E8B-9A63-2D5F0C8B1E47}
AppName=SR20 Realism Mod
AppVersion={#AppVersion}
AppVerName=SR20 Realism Mod {#AppVersion}
AppPublisher=wazooda
DefaultDirName={localappdata}\Programs\SR20 Companion
DisableDirPage=yes
DefaultGroupName=SR20 Realism Mod
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=SR20RealismMod-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile={#Stage}\docs\LICENSE.txt
InfoBeforeFile={#Stage}\docs\BEFORE_YOU_INSTALL.txt
UninstallDisplayName=SR20 Realism Mod
UninstallDisplayIcon={app}\SR20Companion.exe
CloseApplications=no
SetupLogging=yes

[Types]
Name: "full"; Description: "Aircraft and SR20 Companion (recommended)"
Name: "aircraft"; Description: "Aircraft only"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
Name: "aircraft"; Description: "SR20 aircraft (required)"; Types: full aircraft custom; Flags: fixed
Name: "companion"; Description: "SR20 Companion: terrain alerts (TAWS), glide ring, airport maps, iPad link"; Types: full
Name: "companion\yoke"; Description: "RealSimGear Cirrus yoke force feel (only matters with that yoke)"; Types: full

[Tasks]
Name: "startup"; Description: "Start the SR20 Companion with Windows"; Components: companion
Name: "startnow"; Description: "Start the SR20 Companion now"; Components: companion
Name: "ipad"; Description: "Open the iPad setup page when setup finishes"; Components: companion; Flags: unchecked

[Files]
Source: "{#Stage}\package\{#PkgName}\*"; DestDir: "{code:CommunityDir}\{#PkgName}"; Components: aircraft; \
  Flags: ignoreversion recursesubdirs createallsubdirs uninsneveruninstall
; uninsneveruninstall: the uninstaller must not delete these by name (a link put in the package's place later would
; send the deletes into someone's source tree); CurUninstallStepChanged removes the folder after checking for a link
Source: "{#Stage}\SR20Companion\*"; DestDir: "{app}"; Components: companion; \
  Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Stage}\docs\*"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\SR20 Realism Mod\SR20 Companion"; Filename: "{app}\SR20Companion.exe"; Components: companion
Name: "{userprograms}\SR20 Realism Mod\SR20 iPad setup"; Filename: "{app}\SR20Companion.exe"; Parameters: "--ipad"; \
  Components: companion
Name: "{userprograms}\SR20 Realism Mod\Read me"; Filename: "{app}\docs\README.txt"
Name: "{userprograms}\SR20 Realism Mod\Uninstall SR20 Realism Mod"; Filename: "{uninstallexe}"
; the same shortcut the tray's "Start with Windows" tick manages (tools/companion/tray.py SHORTCUT_NAME)
Name: "{userstartup}\SR20 Companion"; Filename: "{app}\SR20Companion.exe"; Parameters: "--background"; \
  WorkingDir: "{app}"; Comment: "SR20 Companion: TAWS, glide ring, airport maps, iPad link, yoke feel"; \
  Components: companion; Tasks: startup

[Run]
Filename: "{app}\SR20Companion.exe"; Parameters: "--background"; WorkingDir: "{app}"; Flags: nowait runasoriginaluser; \
  Components: companion; Tasks: startnow
Filename: "{app}\SR20Companion.exe"; Parameters: "--ipad"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent; \
  Description: "Open the iPad setup page"; Components: companion; Tasks: ipad

[UninstallRun]
Filename: "{app}\SR20Companion.exe"; Parameters: "--quit"; Flags: runhidden waituntilterminated; RunOnceId: "QuitCompanion"

[UninstallDelete]
Type: files; Name: "{userstartup}\SR20 Companion.lnk"

[Code]
var
  CommunityPage: TInputDirWizardPage;
  Installed: Boolean;      { set once the new files are all in place (ssPostInstall) }
  BackedUp: Boolean;       { this run moved the old package aside (only then is it ever restored) }
  PrevDir: String;         { the Community2024 folder of the previous install, read before setup records the new one }

{ UserCfg.opt: InstalledPackagesPath "X:\...\Packages" -> X:\...\Packages\Community2024 }
function PackagesPathFrom(const Cfg: String): String;
var
  Lines: TArrayOfString;
  I, P: Integer;
  S: String;
begin
  Result := '';
  if not LoadStringsFromFile(Cfg, Lines) then Exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    S := Trim(Lines[I]);
    if Pos('InstalledPackagesPath', S) = 1 then
    begin
      P := Pos('"', S);
      if P > 0 then
      begin
        S := Copy(S, P + 1, Length(S));
        P := Pos('"', S);
        if P > 0 then
        begin
          Result := Copy(S, 1, P - 1);
          Exit;
        end;
      end;
    end;
  end;
end;

function DetectCommunity(): String;
var
  P: String;
begin
  P := PackagesPathFrom(ExpandConstant('{userappdata}\Microsoft Flight Simulator 2024\UserCfg.opt'));
  if P = '' then
    P := PackagesPathFrom(ExpandConstant('{localappdata}\Packages\Microsoft.Limitless_8wekyb3d8bbwe\LocalCache\UserCfg.opt'));
  if P <> '' then
    Result := AddBackslash(P) + 'Community2024'
  else if DirExists(ExpandConstant('{userappdata}\Microsoft Flight Simulator 2024\Packages')) then
    Result := ExpandConstant('{userappdata}\Microsoft Flight Simulator 2024\Packages\Community2024')
  else if DirExists(ExpandConstant('{localappdata}\Packages\Microsoft.Limitless_8wekyb3d8bbwe\LocalCache\Packages')) then
    Result := ExpandConstant('{localappdata}\Packages\Microsoft.Limitless_8wekyb3d8bbwe\LocalCache\Packages\Community2024')
  else
    Result := '';
end;

{ junction / symlink: FILE_ATTRIBUTE_REPARSE_POINT }
function IsLink(const Path: String): Boolean;
var
  F: TFindRec;
begin
  Result := False;
  if FindFirst(RemoveBackslashUnlessRoot(Path), F) then
  begin
    Result := (F.Attributes and $400) <> 0;
    FindClose(F);
  end;
end;

function CommunityDir(Param: String): String;
begin
  Result := RemoveBackslashUnlessRoot(CommunityPage.Values[0]);
end;

{ uninstall: the folder recorded at install time }
function InstalledCommunityDir(Param: String): String;
begin
  Result := GetPreviousData('CommunityDir', '');
  if Result = '' then
    Result := ExpandConstant('{tmp}\sr20-nothing-to-remove');
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'CommunityDir', CommunityDir(''));
end;

function SimRunning(): Boolean;
var
  Code: Integer;
begin
  { tasklist exits 0 either way; findstr exits 0 only when the sim's process is listed }
  Result := Exec(ExpandConstant('{cmd}'), '/C tasklist /FI "IMAGENAME eq FlightSimulator2024.exe" /NH | findstr /I FlightSimulator2024.exe >NUL',
                 '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  while SimRunning() do
  begin
    if MsgBox('Microsoft Flight Simulator 2024 is running. Close the sim first, then press Retry.',
              mbError, MB_RETRYCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

procedure InitializeWizard();
var
  Prev: String;
begin
  CommunityPage := CreateInputDirPage(wpSelectComponents, 'Where is MSFS 2024?',
    'The SR20 goes into the sim''s Community2024 folder.',
    'Setup found this folder. Change it only if you keep your sim packages somewhere else. ' +
    'It must be Community2024, not Community (the sim treats Community as MSFS 2020 add-ons and ignores the SR20 there).',
    False, '');
  CommunityPage.Add('Community2024 folder:');
  Prev := GetPreviousData('CommunityDir', '');
  PrevDir := RemoveBackslashUnlessRoot(Prev);      { kept: setup records the new folder before ssPostInstall }
  if Prev <> '' then
    CommunityPage.Values[0] := Prev
  else
    CommunityPage.Values[0] := DetectCommunity();
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  D: String;
begin
  Result := True;
  if CurPageID = CommunityPage.ID then
  begin
    D := RemoveBackslashUnlessRoot(CommunityPage.Values[0]);
    if D = '' then
    begin
      MsgBox('Setup could not find MSFS 2024. Browse to your Community2024 folder.', mbError, MB_OK);
      Result := False;
    end
    else if CompareText(ExtractFileName(D), 'Community') = 0 then
    begin
      MsgBox('That is the Community folder, which the sim uses for MSFS 2020 add-ons. Choose Community2024 (next to it).',
             mbError, MB_OK);
      Result := False;
    end
    else if IsLink(D + '\{#PkgName}') then
    begin
      { a developer's junction into a source tree: replacing it would delete the source }
      MsgBox('Community2024\{#PkgName} is a link to another folder (a development copy). Remove the link first.',
             mbError, MB_OK);
      Result := False;
    end
    else if not DirExists(ExtractFileDir(D)) then
    begin
      MsgBox('The folder above it (' + ExtractFileDir(D) + ') does not exist. Is the sim installed?', mbError, MB_OK);
      Result := False;
    end
    else if CompareText(ExtractFileName(D), 'Community2024') <> 0 then
      Result := MsgBox('The folder is not named Community2024. Install there anyway?', mbConfirmation, MB_YESNO) = IDYES;
  end;
end;

{ stop a running Companion (any copy: --quit signals the per-user quit event) before its files are replaced;
  -> True when no Companion is left running (its single-instance mutex, sr20_companion.py, is gone) }
function StopCompanion(): Boolean;
var
  Code: Integer;
  Exe: String;
begin
  Exe := ExpandConstant('{app}\SR20Companion.exe');
  if FileExists(Exe) and CheckForMutexes('Local\SR20Companion') then
    Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := not CheckForMutexes('Local\SR20Companion');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if SimRunning() then
    Result := 'Microsoft Flight Simulator 2024 is running. Close it and run setup again.'
  else if IsLink(CommunityDir('') + '\{#PkgName}') then
    { a developer's junction into a source tree: installing through it would overwrite the source }
    Result := CommunityDir('') + '\{#PkgName} is a link to another folder (a development copy). Remove the link first.'
  else if (PrevDir <> '') and IsLink(PrevDir + '\{#PkgName}') then
    Result := PrevDir + '\{#PkgName} is a link to another folder (a development copy). Remove the link first.'
  else if not StopCompanion() then
    Result := 'The SR20 Companion is still running. Right-click its icon by the clock, choose Quit SR20 Companion, ' +
              'then run setup again.'
  else if SimRunning() then      { started while the Companion was stopping }
    Result := 'Microsoft Flight Simulator 2024 is running. Close it and run setup again.';
end;

{ JSON string body: backslash and quote escaped, control characters dropped (a Windows path has neither quotes nor
  control characters, but a pasted folder name could) }
function JsonText(const S: String): String;
var
  I: Integer;
  C: String;
begin
  Result := '';
  for I := 1 to Length(S) do
  begin
    C := Copy(S, I, 1);
    if C = '\' then Result := Result + '\\'
    else if C = '"' then Result := Result + '\"'
    else if Ord(S[I]) >= 32 then Result := Result + C;
  end;
end;

{ the Companion reads %LOCALAPPDATA%\SR20Companion\settings.json (sr20_companion.load_settings): the yoke choice and
  the Community2024 folder chosen here, so a folder picked with Browse reaches the Companion too. Written at ssInstall,
  before [Run] starts the Companion; a failed write stops setup (the Companion would run with the wrong choices). }
procedure WriteSettings();
var
  Dir, V: String;
  Lines: TArrayOfString;
begin
  Dir := ExpandConstant('{localappdata}\SR20Companion');
  ForceDirectories(Dir);
  if WizardIsComponentSelected('companion\yoke') then V := 'true' else V := 'false';
  SetArrayLength(Lines, 1);
  Lines[0] := '{"yoke_feel": ' + V + ', "community_dir": "' + JsonText(CommunityDir('')) + '"}';
  if not SaveStringsToUTF8FileWithoutBOM(Dir + '\settings.json', Lines, False) then
    RaiseException('Setup could not save ' + Dir + '\settings.json. Nothing was changed.');
end;

{ the old package moved aside next to it (a rename, so same volume), with its manifest renamed so the sim does not
  load the backup as a second package if setup is killed before it can clean up }
function BackupDir(): String;
begin
  Result := CommunityDir('') + '\{#PkgName}.sr20-backup';
end;

procedure DisableManifest(const Dir: String);
begin
  if FileExists(Dir + '\manifest.json') then
    if not RenameFile(Dir + '\manifest.json', Dir + '\manifest.json.sr20-backup') then
      RaiseException('Setup could not disable the backup''s manifest in ' + Dir + '. Nothing was changed.');
end;

procedure EnableManifest(const Dir: String);
begin
  if FileExists(Dir + '\manifest.json.sr20-backup') and not FileExists(Dir + '\manifest.json') then
    RenameFile(Dir + '\manifest.json.sr20-backup', Dir + '\manifest.json');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Old: String;
begin
  if CurStep = ssInstall then
  begin
    Old := CommunityDir('') + '\{#PkgName}';
    { a backup left by an earlier setup that was killed: if the package is gone it is the only copy, put it back;
      otherwise the package is newer and the old backup goes }
    if DirExists(BackupDir()) then
    begin
      if not DirExists(Old) then
      begin
        EnableManifest(BackupDir());
        if not RenameFile(BackupDir(), Old) then
          RaiseException('Setup found an old backup at ' + BackupDir() + ' and could not restore it. Nothing was changed.');
      end
      else if not DelTree(BackupDir(), True, True, True) then
        RaiseException('Setup could not remove the old backup ' + BackupDir() + '. Nothing was changed.');
    end;
    { a re-install over an older copy: move it aside and put it back if setup fails; the new package starts clean }
    if DirExists(Old) and not IsLink(Old) then
    begin
      if not RenameFile(Old, BackupDir()) then
        RaiseException('Setup could not move the old ' + Old + ' aside (is a file in it open?). Nothing was changed.');
      BackedUp := True;
      DisableManifest(BackupDir());
    end;
    if WizardIsComponentSelected('companion') then
      WriteSettings();
  end;
  if CurStep = ssPostInstall then
  begin
    Installed := True;
    if BackedUp and DirExists(BackupDir()) then
      if not DelTree(BackupDir(), True, True, True) then
        MsgBox('The SR20 is installed, but setup could not delete the old copy at ' + BackupDir() + '. ' +
               'Delete that folder yourself (the sim ignores it meanwhile).', mbInformation, MB_OK);
    { installed to another folder last time: remove that copy, or the sim lists the SR20 twice }
    if (PrevDir <> '') and (CompareText(PrevDir, CommunityDir('')) <> 0) then
    begin
      Old := PrevDir + '\{#PkgName}';
      if DirExists(Old) and not IsLink(Old) then
        DelTree(Old, True, True, True);
    end;
    if WizardIsComponentSelected('companion') and not WizardIsTaskSelected('startup') then
      DeleteFile(ExpandConstant('{userstartup}\SR20 Companion.lnk'));
  end;
end;

{ setup cancelled or failed after this run moved the old package aside: put it back }
procedure DeinitializeSetup();
var
  Pkg: String;
begin
  if Installed or not BackedUp or (CommunityPage = nil) then Exit;
  if not DirExists(BackupDir()) then Exit;
  Pkg := CommunityDir('') + '\{#PkgName}';
  if DirExists(Pkg) and not IsLink(Pkg) then
    DelTree(Pkg, True, True, True);
  EnableManifest(BackupDir());
  if DirExists(Pkg) or not RenameFile(BackupDir(), Pkg) then
    MsgBox('Setup did not finish and could not put the previous SR20 back. It is in ' + BackupDir() +
           ': rename that folder to {#PkgName} (and manifest.json.sr20-backup inside it to manifest.json).',
           mbError, MB_OK);
end;

function UninstallBlocked(): String;
var
  Pkg: String;
begin
  Result := '';
  { the aircraft files are not in the uninstall log (uninsneveruninstall): only this guarded folder delete removes them }
  Pkg := InstalledCommunityDir('') + '\{#PkgName}';
  if IsLink(Pkg) then
    Result := Pkg + ' is now a link to another folder (a development copy). Remove the link, then uninstall.'
  else if SimRunning() then
    Result := 'Microsoft Flight Simulator 2024 is running. Close the sim, then uninstall again.'
  else if CheckForMutexes('Local\SR20Companion') then
    Result := 'The SR20 Companion is still running. Right-click its icon by the clock, choose Quit SR20 Companion, ' +
              'then uninstall again.';
end;

function InitializeUninstall(): Boolean;
var
  Code: Integer;
  Why: String;
begin
  { stop the Companion before anything is removed, so it is not writing airport maps into the package }
  if FileExists(ExpandConstant('{app}\SR20Companion.exe')) and CheckForMutexes('Local\SR20Companion') then
    Exec(ExpandConstant('{app}\SR20Companion.exe'), '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Why := UninstallBlocked();
  if Why <> '' then
    MsgBox(Why, mbError, MB_OK);
  Result := Why = '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data, Why: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    { checked again: the confirmation box may have stayed open while the sim or the Companion was started }
    Why := UninstallBlocked();
    Data := InstalledCommunityDir('') + '\{#PkgName}';
    if Why <> '' then
      MsgBox('The SR20 aircraft was not removed: ' + Why + #13#10 + 'Afterwards delete ' + Data + ' yourself.',
             mbError, MB_OK)
    else if DirExists(Data) then
      DelTree(Data, True, True, True);
    if DirExists(Data + '.sr20-backup') and not IsLink(Data + '.sr20-backup') then
      DelTree(Data + '.sr20-backup', True, True, True);
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{localappdata}\SR20Companion');
    if DirExists(Data) or DirExists(ExpandConstant('{localappdata}\SR20Glide')) then
      if MsgBox('Also delete the SR20 Companion''s saved data (terrain cache, logs, iPad pairing, settings)?' + #13#10 +
                Data + #13#10 + ExpandConstant('{localappdata}\SR20Glide'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        DelTree(Data, True, True, True);
        DelTree(ExpandConstant('{localappdata}\SR20Glide'), True, True, True);
      end;
  end;
end;
