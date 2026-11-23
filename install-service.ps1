<#
.SYNOPSIS
    Registers KeySwitcher to start automatically at logon (or removes it).

.DESCRIPTION
    KeySwitcher uses a low-level keyboard hook and a tray icon, so it must run
    in the user's desktop session. A real Windows service runs in session 0 and
    can't see the keyboard or the taskbar, so this script registers a Scheduled
    Task instead:
      - starts at logon of the current user, with no console window (pythonw)
      - runs elevated, so the remapping also works in admin windows
      - restarts automatically if the process crashes
    It also deletes the legacy Startup-folder KeySwitcher.vbs left by older versions,
    so only one instance runs (two instances would undo each other's swap).

.EXAMPLE
    .\install-service.ps1             # install and start
.EXAMPLE
    .\install-service.ps1 -Uninstall  # stop and remove
#>
param(
    [switch]$Uninstall,
    [switch]$FromApp,  # called from the tray menu: don't kill/restart the app, no pause
    [string]$UserId    # used internally when the script relaunches itself elevated
)

$ErrorActionPreference = 'Stop'
$TaskName = 'KeySwitcher'

if (-not $UserId) { $UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name }

# --- Relaunch elevated if needed ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-UserId', "`"$UserId`"")
    if ($Uninstall) { $argList += '-Uninstall' }
    $windowStyle = 'Normal'
    if ($FromApp) { $argList += '-FromApp'; $windowStyle = 'Hidden' }
    try {
        Start-Process powershell.exe -Verb RunAs -ArgumentList $argList -WindowStyle $windowStyle -Wait
    } catch {
        Write-Host 'Admin rights were not granted; nothing changed.'
    }
    exit
}

function Stop-KeySwitcherProcesses {
    Get-CimInstance Win32_Process -Filter "Name LIKE 'python%.exe'" |
        Where-Object { $_.CommandLine -like '*keyswitcher.py*' } |
        ForEach-Object {
            Write-Host "Stopping running instance (PID $($_.ProcessId))"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

$vbsPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\KeySwitcher.vbs'

try {
    if ($Uninstall) {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "Task '$TaskName' removed."
        } else {
            Write-Host "Task '$TaskName' is not installed."
        }
        if (-not $FromApp) { Stop-KeySwitcherProcesses }
        return
    }

    # --- Locate Python (prefer pythonw.exe to avoid a console window) ---
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) { throw 'python not found in PATH. Install Python 3.10+ first.' }
    $pyExe = (& $python -c 'import sys; print(sys.executable)').Trim()
    $pythonw = Join-Path (Split-Path $pyExe) 'pythonw.exe'
    if (-not (Test-Path $pythonw)) { $pythonw = $pyExe }

    $scriptDir = Split-Path -Parent $PSCommandPath
    $script = Join-Path $scriptDir 'keyswitcher.py'
    if (-not (Test-Path $script)) { throw "keyswitcher.py not found in $scriptDir" }

    # --- Remove the legacy Startup-folder launcher and running instances ---
    if (Test-Path $vbsPath) {
        Remove-Item $vbsPath -Force
        Write-Host 'Removed legacy Startup-folder launcher (KeySwitcher.vbs).'
    }
    if (-not $FromApp) { Stop-KeySwitcherProcesses }

    # --- Register the scheduled task ---
    $action    = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $scriptDir
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $UserId
    $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description 'Logi MX Keys Mac -> Windows key remapper' -Force | Out-Null

    if ($FromApp) {
        # The app is already running; the task takes over from the next logon.
        Write-Host "Task '$TaskName' installed for $UserId."
    } else {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Task '$TaskName' installed for $UserId and started."
    }
    Write-Host "  Python: $pythonw"
    Write-Host "  Script: $script"
}
catch {
    Write-Host "ERROR: $_" -ForegroundColor Red
}
finally {
    # Keep the elevated window open so the output can be read
    if ($PSBoundParameters.ContainsKey('UserId') -and -not $FromApp) { Read-Host 'Press Enter to close' }
}
