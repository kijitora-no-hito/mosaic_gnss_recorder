; mosaic_gnss_recorder のインストーラ。build.ps1 から使う。
#ifndef DriverGlob
  #define DriverGlob "{sys}\DriverStore\FileRepository\septentrio-usbcom.inf_*"
#endif
#define RxToolsUrl "https://www.septentrio.com/en/products/gps-gnss-receiver-software/rxtools"

[Setup]
AppId={{C4A19E37-6F2B-4D08-B1E5-92A7D3F06C18}
AppName=mosaic_gnss_recorder
AppVersion=1.0.0
DefaultDirName={autopf}\mosaic_gnss_recorder
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=mosaic_gnss_recorder_Setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\mosaic_gnss_recorder.exe

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Files]
Source: "dist\mosaic_gnss_recorder\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\mosaic_gnss_recorder"; Filename: "{app}\mosaic_gnss_recorder.exe"

[Run]
Filename: "{app}\mosaic_gnss_recorder.exe"; Description: "{cm:LaunchProgram,mosaic_gnss_recorder}"; Flags: nowait postinstall skipifsilent

[Code]
function DriverInstalled: Boolean;
var
  FindRec: TFindRec;
begin
  Result := FindFirst(ExpandConstant('{#DriverGlob}'), FindRec);
  if Result then
    FindClose(FindRec);
end;

procedure OpenRxToolsPage(Sender: TObject);
var
  ErrorCode: Integer;
begin
  ShellExec('open', '{#RxToolsUrl}', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;

procedure InitializeWizard;
var
  Page: TOutputMsgWizardPage;
  Button: TNewButton;
begin
  if DriverInstalled then
  begin
    Log('Septentrio USB driver: found');
    Exit;
  end;
  Log('Septentrio USB driver: missing');
  Page := CreateOutputMsgPage(wpWelcome, 'Septentrio USB ドライバのインストール',
    'USB ドライバが見つかりませんでした',
    'mosaic_gnss_recorder には Septentrio の USB ドライバを同梱していません。' + #13#10 + #13#10 +
    'Septentrio の公式ページから RxTools を入手し、「USB Driver」を選択した状態でインストールしてください。' +
    'その後、受信機を USB から抜いて接続し直すと、ドライバが適用されます。' + #13#10 + #13#10 +
    'mosaic_gnss_recorder のインストールは、ドライバより先に進めてもかまいません。');
  Button := TNewButton.Create(Page);
  Button.Parent := Page.Surface;
  Button.Caption := '公式ページを開く';
  Button.Top := Page.MsgLabel.Top + Page.MsgLabel.Height + ScaleY(16);
  Button.Width := ScaleX(160);
  Button.Height := ScaleY(28);
  Button.OnClick := @OpenRxToolsPage;
end;
