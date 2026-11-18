param(
    [string]$Version = "final",
    [string]$Python = "",
    [switch]$Gpu,
    [switch]$Publish,
    [switch]$Locked
)
$ErrorActionPreference = "Stop"
$primaryRoot = Split-Path -Parent $PSScriptRoot
$targetRoot = $primaryRoot
try {
    if ($Version -ne "final") {
        if ($Version -notmatch '^v\d+\.\d+(?:\.\d+)?(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?$') {
            throw "Invalid version name. Use v0.1-baseline or v0.9-rc."
        }
        $releaseRoot = Join-Path (Split-Path -Parent $primaryRoot) ((Split-Path -Leaf $primaryRoot) + "_Releases")
        $targetRoot = Join-Path $releaseRoot $Version
        if (-not (Test-Path -LiteralPath (Join-Path $targetRoot "main.py"))) { throw "Release does not exist: $targetRoot" }
        $checkPython = Join-Path $primaryRoot ".venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $checkPython)) { throw "Set up the final version first before setting up a fallback." }
        & $checkPython (Join-Path $primaryRoot "scripts\release_manager.py") verify $Version
        if ($LASTEXITCODE -ne 0) { throw "Release hash verification failed." }
    }
    $environmentDir = Join-Path $targetRoot ".venv"
    $environmentPython = Join-Path $environmentDir "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $environmentPython)) {
        if (Test-Path -LiteralPath $environmentDir) { throw "Existing .venv is incomplete. Rename it manually before setup; it will not be overwritten." }
        if (-not $Python) {
            $candidates = @(@('py','-3.13'), @('py','-3.12'), @('py','-3.11'), @('python'), @('python3'))
            foreach ($candidate in $candidates) {
                $candidate = @($candidate)
                $command = Get-Command $candidate[0] -ErrorAction SilentlyContinue
                if (-not $command -or $command.Source -like '*WindowsApps*') { continue }
                $probeArgs = @($candidate | Select-Object -Skip 1) + @('-c', "import sys; assert (3,11) <= sys.version_info < (3,14); print(sys.executable)")
                $found = & $command.Source @probeArgs 2>$null
                if ($LASTEXITCODE -eq 0 -and $found) { $Python = ($found | Select-Object -Last 1).Trim(); break }
            }
            if (-not $Python) { throw "No supported Python found. Use the Windows client ZIP (includes Python), or install Python 3.11-3.13 x64 from https://www.python.org/downloads/windows/ and retry." }
        }
        & $Python -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ is required'"
        if ($LASTEXITCODE -ne 0) { throw "Python 3.11+ is required. Try -Python C:\path\to\python.exe" }
        & $Python -m venv $environmentDir
        if ($LASTEXITCODE -ne 0) { throw "Failed to create the virtual environment." }
    }
    & $environmentPython -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ is required'"
    if ($LASTEXITCODE -ne 0) { throw "The existing environment requires Python 3.11 or newer." }
    $requirements = Join-Path $targetRoot "requirements.txt"
    if ($Locked) {
        $requirements = Join-Path $targetRoot "requirements.lock.txt"
        if (-not (Test-Path -LiteralPath $requirements)) { throw "Pinned dependency snapshot is missing." }
    }
    & $environmentPython -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Existing configuration and data were preserved." }
    if ($Gpu) {
        $gpuRequirements = Join-Path $targetRoot "requirements-gpu.txt"
        if (-not (Test-Path -LiteralPath $gpuRequirements)) { throw "This release does not include pinned GPU runtime requirements. Use a GPU-capable release or run setup without -Gpu." }
        & $environmentPython -c "import struct, sys, sysconfig; assert sys.platform == 'win32' and sysconfig.get_platform() == 'win-amd64' and struct.calcsize('P') == 8, 'Pinned GPU runtime installation requires 64-bit Windows Python'"
        if ($LASTEXITCODE -ne 0) { throw "This GPU runtime package is tested only on Windows x64." }
        & $environmentPython -m pip install -r $gpuRequirements
        if ($LASTEXITCODE -ne 0) { throw "GPU runtime installation failed. CPU mode and existing project data remain available." }
        Write-Host "Installed project GPU runtime packages. A compatible NVIDIA driver is still required; restart the app before using CUDA."
    }
    if ($Publish) {
        & $environmentPython -m pip install -r (Join-Path $targetRoot "requirements-publish.txt")
        if ($LASTEXITCODE -ne 0) { throw "Publishing dependency installation failed. Local editing remains available." }
        Write-Host "Publishing client installed. Install Microsoft Edge from its official vendor if it is missing."
    }
    $envFile = Join-Path $targetRoot ".env"
    if ($Version -eq "final" -and -not (Test-Path -LiteralPath $envFile)) {
        Copy-Item -LiteralPath (Join-Path $targetRoot ".env.example") -Destination $envFile
        Write-Host "Created .env from .env.example. API configuration is optional; you can open the workbench without an API key."
    }
    Write-Host "Ready: $targetRoot"
    Write-Host "Launch from the primary folder: .\launch.cmd --version $Version"
    exit 0
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
