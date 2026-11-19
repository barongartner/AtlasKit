# Atlas installer. On a new PC, open PowerShell and paste:
#   irm https://raw.githubusercontent.com/barongartner/AtlasKit/main/Install.ps1 | iex
# It installs Git, downloads Atlas to C:\Atlas, runs Setup.cmd (Python, the
# packages, then the Atlas app's walkthrough) and turns on automatic updates.
# Safe to run again: it skips whatever is already done.
# Wrapped so a failure stops the install without closing the PowerShell window.
try {
& {
  $ErrorActionPreference = 'Continue'
  $Dir = 'C:\Atlas'
  if ($env:ATLAS_INSTALL_DIR) { $Dir = $env:ATLAS_INSTALL_DIR }   # testing only
  $TestRun = [bool]$env:ATLAS_INSTALL_TEST                          # testing only: no setup app, no task
  $Repo = 'https://github.com/barongartner/AtlasKit.git'
  function Say($t) { Write-Host ''; Write-Host $t -ForegroundColor Cyan }
  function Fail($t) { Write-Host ''; Write-Host $t -ForegroundColor Red; Write-Host ''; throw 'Atlas install stopped.' }
  function Find-Git {
    $g = Get-Command git -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
    foreach ($p in @("$env:ProgramFiles\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe")) {
      if (Test-Path $p) { return $p }
    }
    return $null
  }

  Say 'Atlas installer'

  # 1. Git
  $git = Find-Git
  if (-not $git) {
    Say 'Installing Git. Windows may ask for permission.'
    winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements --silent
    $git = Find-Git
    if (-not $git) { Fail "Git didn't install. Get it from git-scm.com, then run the install line again." }
  }
  Write-Host "Git: $git"

  # 2. The code
  if (Test-Path "$Dir\.git") {
    $origin = (& $git -C $Dir remote get-url origin 2>$null)
    if ($origin -notmatch 'AtlasKit') { Fail "$Dir is a different project ($origin). Not touching it." }
    Say "Atlas is already in $Dir. Keeping it; it updates itself."
  } elseif ((Test-Path $Dir) -and (Get-ChildItem $Dir -Force | Select-Object -First 1)) {
    Fail "$Dir already exists and isn't Atlas. Rename or move that folder, then run the install line again."
  } else {
    Say "Downloading Atlas to $Dir"
    & $git clone $Repo $Dir
    if ($LASTEXITCODE -ne 0) { Fail "The download failed. Check the internet connection and run this again." }
  }

  if ($TestRun) { Say 'Test run: stopping before setup.'; return }

  # 3. Python, packages, and the setup app
  Say 'Setting up Python and the packages (a few minutes the first time)'
  Push-Location $Dir
  & cmd /c Setup.cmd
  $setupCode = $LASTEXITCODE
  Pop-Location
  if ($setupCode -ne 0) { Fail 'Setup.cmd stopped with an error (shown above). Fix that, then run the install line again.' }

  # 4. Automatic updates
  $py = $null
  if (Get-Command py -ErrorAction SilentlyContinue) { $py = (& py -3.12 -c "import sys;print(sys.executable)") }
  if (-not $py -or -not (Test-Path $py)) { $py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" }
  if (Test-Path $py) {
    & $py "$Dir\Shared\update.py" --install-task | Out-Null
    if ($LASTEXITCODE -eq 0) { Say 'Automatic updates are on (every 30 minutes).' }
    else { Say "Couldn't make the update task now. The setup app's last step makes it." }
  } else {
    Say "Couldn't find Python for the update task. The setup app's last step makes it."
  }

  Say 'Done here. The Atlas app is open: follow it step by step. You can close this window.'
}
} catch { Write-Host $_ -ForegroundColor Red }
