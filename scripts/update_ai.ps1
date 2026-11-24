<#
Keeps the local AI fast: updates Ollama, checks the model runs on the GPU,
and goes back to the last version that worked if the new one can't use it.
Then opens the NVIDIA app so you can install a newer driver (NVIDIA has no
safe silent driver update, so that one click stays yours).

  update-ai.bat              update Ollama if there is a new version, test the GPU
  update-ai.bat -Repair      reinstall Ollama even if it is up to date
  update-ai.bat -Restore     put back the last Ollama that worked on the GPU
  update-ai.bat -NoNvidia    skip the NVIDIA step

The GPU test and a broken GPU also trigger a reinstall (the fix that worked
when a Windows update broke Ollama's CUDA files). Only the NUMBER of the last
version that worked is kept (%LOCALAPPDATA%\Ollama-last-good.txt); going back
downloads that version's installer again from Ollama's GitHub releases, so
no copy of Ollama takes up disk space. Models (%USERPROFILE%\.ollama) are
never touched.
#>
param([switch]$Repair, [switch]$Restore, [switch]$NoNvidia, [switch]$NoPause, [string]$LogFile = "")

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # the progress bar makes downloads much slower

$AppDir = Split-Path -Parent $PSScriptRoot
$OllamaDir = Join-Path $env:LOCALAPPDATA "Programs\Ollama"
$Exe = Join-Path $OllamaDir "ollama.exe"
$Api = "http://127.0.0.1:11434"
$LastGood = Join-Path $env:LOCALAPPDATA "Ollama-last-good.txt"
$OllamaLog = Join-Path $AppDir "data\logs\ollama.log"
$NvidiaApp = "C:\Program Files\NVIDIA Corporation\NVIDIA app\CEF\NVIDIA app.exe"
if (-not $LogFile) { $LogFile = Join-Path $AppDir "data\logs\update-ai.log" }
New-Item -ItemType Directory -Force (Split-Path $LogFile) | Out-Null
[IO.File]::WriteAllText($LogFile, "")

function Say([string]$msg) {
  $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
  Write-Host $line
  [IO.File]::AppendAllText($LogFile, $line + "`r`n")
}

function Get-OllamaVersion {
  if (-not (Test-Path $Exe)) { return $null }
  $out = (& $Exe --version 2>&1 | Out-String)
  $m = [regex]::Matches($out, '\d+\.\d+\.\d+')
  if ($m.Count) { return $m[$m.Count - 1].Value } else { return $null }
}

function Get-LatestVersion {
  try {
    $r = Invoke-RestMethod "https://api.github.com/repos/ollama/ollama/releases/latest" -Headers @{ "User-Agent" = "oppo-updater" } -TimeoutSec 20
    return $r.tag_name.TrimStart("v")
  } catch { Say "Could not ask GitHub for the latest version: $($_.Exception.Message)"; return $null }
}

function Test-Server {
  try { Invoke-RestMethod "$Api/api/version" -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

function Wait-Server([int]$seconds) {
  for ($i = 0; $i -lt $seconds; $i++) { if (Test-Server) { return $true }; Start-Sleep 1 }
  return $false
}

function Stop-Ollama {
  Get-Process "ollama app", "ollama", "llama-server" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  for ($i = 0; $i -lt 10 -and (Get-Process "ollama", "llama-server" -ErrorAction SilentlyContinue); $i++) { Start-Sleep 1 }
}

function Start-Ollama {
  if (Test-Server) { return $true }
  New-Item -ItemType Directory -Force (Split-Path $OllamaLog) | Out-Null
  Start-Process cmd -ArgumentList "/c", "`"`"$Exe`" serve >> `"$OllamaLog`" 2>&1`"" -WindowStyle Minimized
  return (Wait-Server 30)
}

# The model the app reads pages with: [llm].extract_model, else [llm].model, from preferences.toml.
function Get-TestModel {
  $installed = @()
  try { $installed = @((Invoke-RestMethod "$Api/api/tags" -TimeoutSec 10).models | ForEach-Object { $_.name }) } catch {}
  $prefs = Join-Path $AppDir "preferences.toml"
  $wanted = @()
  if (Test-Path $prefs) {
    $text = [IO.File]::ReadAllText($prefs)
    $llm = [regex]::Match($text, '(?ms)^\[llm\]\s*$(.*?)(?=^\[|\z)').Groups[1].Value
    foreach ($key in "extract_model", "model") {
      $m = [regex]::Match($llm, "(?m)^\s*$key\s*=\s*`"([^`"]+)`"")
      if ($m.Success) { $wanted += $m.Groups[1].Value }
    }
  }
  foreach ($w in $wanted) {
    foreach ($i in $installed) { if ($i -eq $w -or $i -eq "$($w):latest") { return $i } }
  }
  return ($installed | Where-Object { $_ -notmatch 'embed|bge|nomic|minilm' } | Select-Object -First 1)
}

function Get-NumCtx {
  $prefs = Join-Path $AppDir "preferences.toml"
  if (Test-Path $prefs) {
    $m = [regex]::Match([IO.File]::ReadAllText($prefs), '(?m)^\s*num_ctx\s*=\s*(\d+)')
    if ($m.Success) { return [int]$m.Groups[1].Value }
  }
  return 8192
}

# Share of the model in video memory (1 = all on the GPU), as the app checks it:
# load, and if it isn't on the GPU unload and load once more.
function Test-Gpu([string]$model) {
  $ctx = Get-NumCtx
  for ($try = 0; $try -lt 2; $try++) {
    try {
      $body = @{ model = $model; keep_alive = "2m"; options = @{ num_ctx = $ctx } } | ConvertTo-Json
      Invoke-RestMethod "$Api/api/generate" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 600 | Out-Null
      $m = (Invoke-RestMethod "$Api/api/ps" -TimeoutSec 10).models | Where-Object { $_.name -eq $model } | Select-Object -First 1
    } catch { Say "Could not load $($model): $($_.Exception.Message)"; return $null }
    if (-not $m -or -not $m.size) { return $null }
    $share = [double]$m.size_vram / [double]$m.size
    if ($share -ge 0.9) { return $share }
    $unload = @{ model = $model; keep_alive = 0 } | ConvertTo-Json
    try { Invoke-RestMethod "$Api/api/generate" -Method Post -Body $unload -ContentType "application/json" -TimeoutSec 60 | Out-Null } catch {}
  }
  return $share
}

function Describe([object]$share) {
  if ($null -eq $share) { return "could not test" }
  $pct = [string][math]::Round([double]$share * 100) + "%"
  if ($share -ge 0.9) { return "$pct on the GPU" }
  if ($share -lt 0.05) { return "100% on the CPU (slow)" }
  return "only $pct on the GPU"
}

# Ollama's GPU part needs the Microsoft Visual C++ runtime 14.40 or newer in
# System32. On 17 Nov 2026 an old 14.13 copy (from 2017) had overwritten it
# while Windows still listed 14.51 as installed: llama-server crashed
# (0xc0000005 in MSVCP140.dll) and every model ran on the CPU.
function Test-VcRuntime {
  $dll = Join-Path $env:WINDIR "System32\msvcp140.dll"
  if (-not (Test-Path $dll)) { return "missing" }
  $v = (Get-Item $dll).VersionInfo
  $ver = [version]("{0}.{1}" -f $v.FileMajorPart, $v.FileMinorPart)
  if ($ver -lt [version]"14.40") { return "$($v.FileMajorPart).$($v.FileMinorPart).$($v.FileBuildPart)" }
  return $null
}

function Save-LastGood([string]$version) {
  if ((Test-Path $LastGood) -and ((Get-Content $LastGood -Raw).Trim() -eq $version)) { return }
  Set-Content $LastGood $version -Encoding ASCII
  Say "Noted $version as the last Ollama that works on the GPU."
}

# Downloads the last version that worked from Ollama's GitHub releases and installs it.
function Restore-LastGood {
  if (-not (Test-Path $LastGood)) { Say "No Ollama version has been noted as working on the GPU yet."; return $false }
  $v = (Get-Content $LastGood -Raw).Trim()
  Say "Putting back Ollama $v (downloaded again from GitHub) ..."
  Install-Ollama "https://github.com/ollama/ollama/releases/download/v$v/OllamaSetup.exe"
  return $true
}

function Install-Ollama([string]$url = "https://ollama.com/download/OllamaSetup.exe") {
  $setup = Join-Path $env:TEMP "OllamaSetup.exe"
  Say "Downloading the Ollama installer (about 1.6-1.8 GB) from $url ..."
  Invoke-WebRequest $url -OutFile $setup -UseBasicParsing
  $sig = Get-AuthenticodeSignature $setup
  if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Ollama") {
    Remove-Item $setup -Force -ErrorAction SilentlyContinue
    throw "The installer's signature is not a valid Ollama one ($($sig.Status)): not installing it."
  }
  Say "Installer signed by $($sig.SignerCertificate.Subject.Split(',')[0]). Installing ..."
  Stop-Ollama
  # WaitForExit, not -Wait: in Windows PowerShell 5.1 -Wait also waits for what
  # the installer starts (Ollama's tray app, which never exits).
  $p = Start-Process $setup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-" -PassThru
  $p.WaitForExit()
  Remove-Item $setup -Force -ErrorAction SilentlyContinue
  if ($p.ExitCode -ne 0) { throw "The installer stopped with code $($p.ExitCode)." }
}

$exitCode = 0
try {
  Say "== Oppo: update Ollama and check the GPU =="
  $hasNvidia = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
  if ($hasNvidia) { Say ("NVIDIA: " + ((& nvidia-smi --query-gpu=name,driver_version --format=csv,noheader) -join "; ")) }
  else { Say "No NVIDIA card found: the GPU test is skipped." }

  $oldRuntime = Test-VcRuntime
  if ($oldRuntime) {
    Say "PROBLEM: Windows' Microsoft Visual C++ runtime (System32\msvcp140.dll) is $oldRuntime; Ollama needs 14.40+."
    Say "  With it Ollama can't use the GPU, whatever its version. Fix (needs administrator):"
    Say "  download https://aka.ms/vs/17/release/vc_redist.x64.exe from Microsoft, run it, choose Repair, restart the PC."
    Say "  Not reinstalling Ollama: it would not help."
  }

  $current = Get-OllamaVersion
  $latest = Get-LatestVersion
  Say "Ollama installed: $(if ($current) { $current } else { 'none' }) / latest: $(if ($latest) { $latest } else { '?' })"

  if ($Restore) {
    if (Restore-LastGood) { Start-Ollama | Out-Null; Say "Ollama is now $(Get-OllamaVersion)." }
  } else {
    $model = $null; $before = $null
    if ($current -and (Start-Ollama)) {
      $model = Get-TestModel
      if ($hasNvidia -and $model) {
        Say "Testing $model on the current Ollama ..."
        $before = Test-Gpu $model
        Say "Now: $(Describe $before)"
      }
    }
    $beforeOk = ($null -ne $before) -and ($before -ge 0.9)
    if ($beforeOk) { Save-LastGood $current }

    $newer = $latest -and $current -and ([version]$latest -gt [version]$current)
    $broken = $hasNvidia -and $model -and -not $beforeOk -and -not $oldRuntime
    if ($newer -or $Repair -or -not $current -or $broken) {
      $why = if ($newer) { "a new version is out" } elseif ($Repair) { "you asked to reinstall" } elseif (-not $current) { "it is not installed" } else { "the model is not on the GPU" }
      Say "Installing Ollama because $why."
      Install-Ollama
      if (-not (Wait-Server 20)) { Start-Ollama | Out-Null }
      $now = Get-OllamaVersion
      Say "Ollama is now $now."
      if (-not $model) { $model = Get-TestModel }
      if ($hasNvidia -and $model) {
        $after = Test-Gpu $model
        Say "After the update: $(Describe $after)"
        if (($null -ne $after) -and $after -ge 0.9) {
          Save-LastGood $now
        } elseif ($beforeOk) {
          Say "The new Ollama can't use the GPU but the old one could: going back."
          if (Restore-LastGood) { Start-Ollama | Out-Null; Say "Back to $(Get-OllamaVersion): $(Describe (Test-Gpu $model))" }
          $exitCode = 2
        } else {
          Say "Still not on the GPU. The NVIDIA driver is the next suspect: the NVIDIA app opens now."
          $exitCode = 2
        }
      }
    } else {
      Say "Ollama is up to date and works on the GPU. Nothing to install."
    }
  }

  if ($hasNvidia -and -not $NoNvidia) {
    if (Test-Path $NvidiaApp) {
      Say "Opening the NVIDIA app: in Drivers, install the update if one is offered, then restart the PC."
      Start-Process $NvidiaApp
    } else {
      Say "NVIDIA app not found: opening the driver page."
      Start-Process "https://www.nvidia.com/en-us/drivers/"
    }
  }
  Say "== Done =="
} catch {
  Say "ERROR: $($_.Exception.Message)"
  if (-not (Test-Server) -and (Test-Path $Exe)) { Start-Ollama | Out-Null }
  $exitCode = 1
}

if (-not $NoPause) { Write-Host ""; Read-Host "Press Enter to close" | Out-Null }
exit $exitCode
