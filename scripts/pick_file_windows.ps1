param(
    [Parameter(Mandatory=$true)][string]$RequestFile,
    [Parameter(Mandatory=$true)][string]$ResultFile
)
$ErrorActionPreference = 'Stop'
$pickerOwner = $null
$pickerDialog = $null
$pickerResult = $null
$pickerExit = 0
$pickerDesktop = ''
function Write-PickerStatus([string]$Stage) {
    $pickerStatusPath = $ResultFile + '.status.json'
    $pickerStatus = @{stage=$Stage;pid=$PID;session=[Diagnostics.Process]::GetCurrentProcess().SessionId;desktop=$pickerDesktop}
    [IO.File]::WriteAllText(($pickerStatusPath + '.partial'), ($pickerStatus | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath ($pickerStatusPath + '.partial') -Destination $pickerStatusPath -Force
}
try {
    Write-PickerStatus 'starting'
    $pickerRequest = [IO.File]::ReadAllText($RequestFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($pickerRequest.kind -notin @('video','folder','srt','json','image')) { throw 'Unsupported file selection type.' }
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.Application]::EnableVisualStyles()
    Write-PickerStatus 'forms-ready'
    if ($pickerRequest.self_test) {
        $pickerResult = @{ready=$true;backend='windows-forms-sta'}
    } else {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class PickerDesktopInfo {
    [DllImport("user32.dll")] static extern IntPtr GetProcessWindowStation();
    [DllImport("user32.dll")] static extern IntPtr GetThreadDesktop(uint id);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern bool GetUserObjectInformation(IntPtr h, int n, StringBuilder s, int size, out int needed);
    static string Name(IntPtr h) { var s=new StringBuilder(256); int needed; return GetUserObjectInformation(h,2,s,512,out needed) ? s.ToString() : "unknown"; }
    public static string Current() { return Name(GetProcessWindowStation()) + "\\" + Name(GetThreadDesktop(GetCurrentThreadId())); }
}
'@
        $pickerDesktop = [PickerDesktopInfo]::Current()
        Write-PickerStatus 'desktop-ready'
        # A genuine taskbar owner is needed for detached web-service launches.
        # Never combine a transparent/withdrawn owner with a hidden startup mode.
        $pickerOwner = New-Object System.Windows.Forms.Form
        $pickerOwner.Text = [string]$pickerRequest.title
        $pickerOwner.ShowInTaskbar = $true
        $pickerOwner.TopMost = $true
        $pickerOwner.StartPosition = 'CenterScreen'
        $pickerOwner.Width = 320
        $pickerOwner.Height = 105
        $pickerLabel = New-Object System.Windows.Forms.Label
        $pickerLabel.Text = 'Choose a local file in the dialog.'
        $pickerLabel.AutoSize = $true
        $pickerLabel.Left = 12
        $pickerLabel.Top = 16
        $pickerOwner.Controls.Add($pickerLabel)
        $pickerOwner.Show()
        $pickerOwner.Activate()
        $pickerOwner.BringToFront()
        [System.Windows.Forms.Application]::DoEvents()
        Write-PickerStatus 'owner-visible'
        $pickerInitial = [string]$pickerRequest.initial_dir
        if (-not $pickerInitial -or -not [IO.Directory]::Exists($pickerInitial)) {
            $pickerInitial = [Environment]::GetFolderPath('MyVideos')
            if (-not [IO.Directory]::Exists($pickerInitial)) { $pickerInitial = [Environment]::GetFolderPath('UserProfile') }
        }
        if ($pickerRequest.kind -eq 'folder') {
            $pickerDialog = New-Object System.Windows.Forms.FolderBrowserDialog
            $pickerDialog.Description = [string]$pickerRequest.title
            $pickerDialog.ShowNewFolderButton = $true
            $pickerDialog.SelectedPath = $pickerInitial
            Write-PickerStatus 'dialog-waiting'
            $pickerChosen = $pickerDialog.ShowDialog($pickerOwner)
            $pickerResult = @{path=$null}
            if ($pickerChosen -eq [System.Windows.Forms.DialogResult]::OK) { $pickerResult.path = $pickerDialog.SelectedPath }
        } else {
            $pickerFilters = @{
                video='Video files|*.mp4;*.mkv;*.flv;*.mov;*.webm;*.ts|All files|*.*'
                srt='SRT subtitles|*.srt|All files|*.*'
                json='Transcript JSON|*.json|All files|*.*'
                image='Cover images|*.jpg;*.jpeg;*.png;*.webp|All files|*.*'
            }
            $pickerDialog = New-Object System.Windows.Forms.OpenFileDialog
            $pickerDialog.Title = [string]$pickerRequest.title
            $pickerDialog.Filter = $pickerFilters[[string]$pickerRequest.kind]
            $pickerDialog.Multiselect = [bool]$pickerRequest.multiple
            $pickerDialog.CheckFileExists = $true
            $pickerDialog.RestoreDirectory = $true
            $pickerDialog.InitialDirectory = $pickerInitial
            Write-PickerStatus 'dialog-waiting'
            $pickerChosen = $pickerDialog.ShowDialog($pickerOwner)
            if ($pickerRequest.multiple) {
                $pickerResult = @{paths=@()}
                if ($pickerChosen -eq [System.Windows.Forms.DialogResult]::OK) { $pickerResult.paths = @($pickerDialog.FileNames) }
            } else {
                $pickerResult = @{path=$null}
                if ($pickerChosen -eq [System.Windows.Forms.DialogResult]::OK) { $pickerResult.path = $pickerDialog.FileName }
            }
        }
    }
} catch {
    Write-PickerStatus 'error'
    $pickerResult = @{error='Native file selection could not open. Close old dialogs and retry, or use the advanced path field.'}
    $pickerExit = 1
} finally {
    if ($null -ne $pickerDialog) { $pickerDialog.Dispose() }
    if ($null -ne $pickerOwner) { $pickerOwner.Close(); $pickerOwner.Dispose() }
}
$pickerTempResult = $ResultFile + '.partial'
[IO.File]::WriteAllText($pickerTempResult, ($pickerResult | ConvertTo-Json -Compress -Depth 5), (New-Object Text.UTF8Encoding($false)))
Move-Item -LiteralPath $pickerTempResult -Destination $ResultFile -Force
Write-PickerStatus 'complete'
exit $pickerExit
