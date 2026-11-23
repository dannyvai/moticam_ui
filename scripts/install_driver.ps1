# Installs the Motic USB camera driver that ships in the "driver" folder next to this script.
#   Right-click > Run with PowerShell, or double-click install_driver.bat.
#   Use -Force to reinstall even if a Motic driver is already present.
param([switch]$Force)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $PSCommandPath
$driverDir = Join-Path $here 'driver'

# Re-launch with administrator rights if needed (Windows shows a UAC prompt).
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($Force) { $argList += ' -Force' }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    exit
}

Write-Host ''
Write-Host 'Motic camera driver installer' -ForegroundColor Cyan
Write-Host '-----------------------------'

$infs = @(Get-ChildItem -Path $driverDir -Filter *.inf -Recurse -ErrorAction SilentlyContinue)
if ($infs.Count -eq 0) {
    Write-Host "No driver files found in $driverDir" -ForegroundColor Red
    pause
    exit 1
}

# Is a Motic driver already in the Windows driver store?
$installed = @(Select-String -Path "$env:windir\INF\oem*.inf" -Pattern 'VID_232F' -SimpleMatch -List `
               -ErrorAction SilentlyContinue)
$devices = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
             Where-Object { $_.InstanceId -match 'VID_232F' })

if ($installed.Count -gt 0 -and -not $Force) {
    Write-Host ("A Motic driver is already installed (" + (($installed | ForEach-Object { $_.Filename }) -join ', ') + ').') -ForegroundColor Green
    foreach ($d in $devices) { Write-Host ("  Camera: {0}  status: {1}" -f $d.FriendlyName, $d.Status) }
    Write-Host 'Nothing to do. Run with -Force to reinstall.'
    pause
    exit 0
}

$failed = $false
foreach ($inf in $infs) {
    Write-Host "Installing $($inf.Name) ..."
    & pnputil.exe /add-driver "$($inf.FullName)" /install
    if ($LASTEXITCODE -eq 3010) {
        Write-Host 'Installed - Windows asks for a restart to finish.' -ForegroundColor Yellow
    } elseif ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 259) {
        Write-Host "pnputil returned code $LASTEXITCODE" -ForegroundColor Red
        $failed = $true
    }
}

Write-Host ''
if ($failed) {
    Write-Host 'The driver could not be installed. Install Motic Images Plus 3.0 instead.' -ForegroundColor Red
} else {
    Write-Host 'Done. Unplug the camera, plug it back in, then start the program.' -ForegroundColor Green
}
pause
