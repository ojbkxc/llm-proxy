[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('add', 'list', 'launch', 'remove', 'sync', 'cleanup', 'reset-protocol', 'help')]
    [string]$Command = 'help',

    [Parameter(Position = 1)]
    [string]$Name,

    [string]$Source,
    [string]$AccountsRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'QoderCN-Multi\accounts'),
    [switch]$NoShortcut,
    [switch]$NoProtocol,
    [switch]$PurgeData,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LaunchVbs = Join-Path $ScriptDir 'QoderCN-Launch.vbs'
$ProtocolVbs = Join-Path $ScriptDir 'QoderCN-Protocol.vbs'
$Wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
$Taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
$RealLauncherDir = Join-Path $env:LOCALAPPDATA 'Qoder CN\Qoder CN Launcher'
$RealLauncherExe = Join-Path $RealLauncherDir 'Qoder CN Launcher.exe'
$RealStateFile = Join-Path $RealLauncherDir 'state.ini'
$ProtocolRoot = 'HKCU:\Software\Classes\qoder-cn'
$ProtocolCommandKey = Join-Path $ProtocolRoot 'shell\open\command'
$AccountsRoot = [IO.Path]::GetFullPath($AccountsRoot).TrimEnd('\')
$TrashRoot = Join-Path (Split-Path -Parent $AccountsRoot) 'trash'
$ProbeRoot = Join-Path $TrashRoot 'probe'
if ($Source) {
    $Source = [IO.Path]::GetFullPath($Source.Trim().Trim('"')).TrimEnd('\')
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Read-Ini {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith('[') -or $t.StartsWith(';') -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -gt 0) {
            $map[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
        }
    }
    return $map
}

function Normalize-AccountId {
    param([string]$Value)
    if (-not $Value) { throw 'Account name is required. Example: add work' }
    if ($Value -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$') {
        throw 'Account name must be 1-32 characters: letters, digits, underscore or dash, and start with a letter or digit.'
    }
    $id = $Value.ToLowerInvariant()
    if ($id -match '^(con|prn|aux|nul|com[1-9]|lpt[1-9])$') {
        throw "Account name '$id' is a reserved Windows device name."
    }
    return $id
}

function Get-AccountDir {
    param([string]$Id)
    return (Join-Path $AccountsRoot $Id)
}

function Get-AccountMeta {
    param([string]$Id)
    $dir = Get-AccountDir $Id
    $metaPath = Join-Path $dir 'account.json'
    if (-not (Test-Path -LiteralPath $metaPath)) {
        throw "Account '$Id' does not exist under $AccountsRoot"
    }
    $meta = Get-Content -Raw -LiteralPath $metaPath | ConvertFrom-Json
    $meta | Add-Member -NotePropertyName accountDir -NotePropertyValue $dir -Force
    return $meta
}

function Get-AccountDataDir {
    param($Meta)
    return (Join-Path ([Environment]::GetFolderPath('ApplicationData')) $Meta.dataDirectoryName)
}

function Get-PrimaryInstall {
    if ($Source) {
        $exe = Join-Path $Source 'Qoder CN.exe'
        if (-not (Test-Path -LiteralPath $exe)) {
            throw "Source installation not found: $exe"
        }
        return $Source
    }
    $state = Read-Ini $RealStateFile
    $candidates = @()
    if ($state.installDir) { $candidates += $state.installDir }
    $candidates += 'C:\Programs\Qoder CN'
    $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Qoder CN')
    $candidates += (Join-Path $env:ProgramFiles 'Qoder CN')
    foreach ($dir in $candidates) {
        if ($dir -and (Test-Path -LiteralPath (Join-Path $dir 'Qoder CN.exe'))) {
            return ([IO.Path]::GetFullPath($dir).TrimEnd('\'))
        }
    }
    throw 'Could not locate the primary Qoder CN installation. Use -Source "C:\path\to\Qoder CN".'
}

function Get-InstallVersion {
    param([string]$InstallDir)
    $manifest = Join-Path $InstallDir 'resources\build-manifest.json'
    if (Test-Path -LiteralPath $manifest) {
        try {
            $obj = Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json
            if ($obj.productVersion) { return [string]$obj.productVersion }
        } catch { }
    }
    $state = Read-Ini $RealStateFile
    if ($state.targetVersion) { return [string]$state.targetVersion }
    return '0.0.0'
}

function Get-ProductConfig {
    param([string]$InstallDir)
    $path = Join-Path $InstallDir 'resources\product.json'
    if (-not (Test-Path -LiteralPath $path)) { throw "product.json not found in $InstallDir" }
    return (Get-Content -Raw -LiteralPath $path | ConvertFrom-Json)
}

function Link-OrCopyFile {
    param([string]$From, [string]$To)
    if (Test-Path -LiteralPath $To) {
        try { Remove-Item -LiteralPath $To -Force -ErrorAction Stop } catch { return 'keep' }
    }
    try {
        New-Item -ItemType HardLink -Path $To -Target $From -ErrorAction Stop | Out-Null
        return 'link'
    } catch {
        Copy-Item -LiteralPath $From -Destination $To -Force -ErrorAction Stop
        return 'copy'
    }
}

function Remove-TreeBestEffort {
    param([string]$Path)
    $result = [pscustomobject]@{ Removed = 0; Remaining = 0 }
    if (-not (Test-Path -LiteralPath $Path)) { return $result }

    foreach ($file in @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        try { Remove-Item -LiteralPath $file.FullName -Force -ErrorAction Stop; $result.Removed++ } catch { }
    }
    $dirs = @(Get-ChildItem -LiteralPath $Path -Recurse -Directory -Force -ErrorAction SilentlyContinue | Sort-Object { $_.FullName.Length } -Descending)
    foreach ($dir in $dirs) {
        try { Remove-Item -LiteralPath $dir.FullName -Force -ErrorAction Stop } catch { }
    }
    if (Test-Path -LiteralPath $Path) {
        try { Remove-Item -LiteralPath $Path -Force -ErrorAction Stop } catch { }
    }
    if (Test-Path -LiteralPath $Path) {
        $result.Remaining = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue).Count
    }
    return $result
}

function Move-ToTrash {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    New-Item -ItemType Directory -Path $TrashRoot -Force | Out-Null
    $safeLabel = ($Label -replace '[^A-Za-z0-9_-]', '_')
    $dest = Join-Path $TrashRoot ('{0}-{1}-{2}' -f $safeLabel, (Get-Date -Format 'yyyyMMdd-HHmmss'), [guid]::NewGuid().ToString('N').Substring(0, 8))
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        try {
            Move-Item -LiteralPath $Path -Destination $dest -Force -ErrorAction Stop
            return $dest
        } catch {
            if ($attempt -eq 6) { throw "Could not move '$Path' out of the way: $($_.Exception.Message)" }
            Start-Sleep -Milliseconds 700
        }
    }
}

function Get-TrashInfo {
    $info = [pscustomobject]@{ Files = 0; Bytes = 0L }
    if (-not (Test-Path -LiteralPath $TrashRoot)) { return $info }
    $files = @(Get-ChildItem -LiteralPath $TrashRoot -Recurse -File -Force -ErrorAction SilentlyContinue)
    $info.Files = $files.Count
    foreach ($file in $files) { $info.Bytes += $file.Length }
    return $info
}

function Clear-Trash {
    if (-not (Test-Path -LiteralPath $TrashRoot)) { return }
    foreach ($dir in @(Get-ChildItem -LiteralPath $TrashRoot -Directory -Force -ErrorAction SilentlyContinue)) {
        [void](Remove-TreeBestEffort -Path $dir.FullName)
    }
    try { Remove-Item -LiteralPath $TrashRoot -Force -ErrorAction Stop } catch { }
}

function Get-ProbePath {
    param([string]$SourcePath, $Item)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $key = $SourcePath.ToLowerInvariant() + '|' + $Item.Length + '|' + $Item.LastWriteTimeUtc.Ticks
        $hash = [BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($key))).Replace('-', '')
    } finally {
        $sha.Dispose()
    }
    return (Join-Path $ProbeRoot ($hash.Substring(0, 32) + '-' + $Item.Name))
}

function Copy-InstallTree {
    param([string]$From, [string]$To)
    $srcRoot = [IO.Path]::GetFullPath($From).TrimEnd('\')
    $script:linkCount = 0
    $script:copyCount = 0
    $script:linkBytes = 0L
    $script:copyBytes = 0L
    New-Item -ItemType Directory -Path $ProbeRoot -Force | Out-Null

    Get-ChildItem -LiteralPath $srcRoot -Recurse -Force | ForEach-Object {
        $rel = $_.FullName.Substring($srcRoot.Length).TrimStart('\')
        $target = Join-Path $To $rel
        if ($_.PSIsContainer) {
            if (-not (Test-Path -LiteralPath $target)) {
                New-Item -ItemType Directory -Path $target -Force | Out-Null
            }
            return
        }
        if ($rel -ieq 'Uninstall Qoder CN.exe' -or $rel -ieq 'resources\product.json') { return }
        $parent = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }

        $length = $_.Length
        $probePath = Get-ProbePath -SourcePath $_.FullName -Item $_
        $probeLocked = $false
        if (Test-Path -LiteralPath $probePath) {
            try { Remove-Item -LiteralPath $probePath -Force -ErrorAction Stop }
            catch { $probeLocked = $true }
        }

        # A hardlink whose file has a memory-mapped section cannot be deleted
        # while Qoder CN runs, so probe with a reusable link and copy if locked.
        if (-not $probeLocked) {
            try {
                New-Item -ItemType HardLink -Path $probePath -Target $_.FullName -ErrorAction Stop | Out-Null
            } catch {
                $probeLocked = $true
            }
            if (-not $probeLocked) {
                try {
                    Remove-Item -LiteralPath $probePath -Force -ErrorAction Stop
                } catch {
                    $probeLocked = $true
                }
            }
        }

        if ($probeLocked) {
            Copy-Item -LiteralPath $_.FullName -Destination $target -Force -ErrorAction Stop
            $script:copyCount++
            $script:copyBytes += $length
        } else {
            try {
                New-Item -ItemType HardLink -Path $target -Target $_.FullName -ErrorAction Stop | Out-Null
                $script:linkCount++
                $script:linkBytes += $length
            } catch {
                Copy-Item -LiteralPath $_.FullName -Destination $target -Force -ErrorAction Stop
                $script:copyCount++
                $script:copyBytes += $length
            }
        }
    }
    return [pscustomobject]@{
        Linked    = $script:linkCount
        Copied    = $script:copyCount
        LinkedMB  = [math]::Round($script:linkBytes / 1MB, 1)
        CopiedMB  = [math]::Round($script:copyBytes / 1MB, 1)
    }
}

function Write-AccountProductJson {
    param([string]$SourceInstall, [string]$AccountAppDir, [string]$Id, [string]$DataDirectoryName)
    $product = Get-ProductConfig -InstallDir $SourceInstall
    $product.dataDirectoryName = $DataDirectoryName
    $product.windowsLauncherMutexName = "Local\QoderSpaceWindowsLauncher-qoder-cn-$Id"
    $json = $product | ConvertTo-Json -Depth 10
    Write-Utf8NoBom -Path (Join-Path $AccountAppDir 'resources\product.json') -Text ($json + "`r`n")
}

function Write-LauncherIniFiles {
    param([string]$LauncherDir, [string]$InstallDir, [string]$Version)
    $now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
    $state = @"
[launcher]
schemaVersion=3
status=healthy
installDir=$InstallDir
appExecutable=Qoder CN.exe
targetVersion=$Version
updatedAt=$now
"@
    $install = @"
[launcher]
schemaVersion=3
status=idle
installDir=$InstallDir
appExecutable=Qoder CN.exe
targetVersion=$Version
"@
    Write-Utf8NoBom -Path (Join-Path $LauncherDir 'state.ini') -Text $state
    Write-Utf8NoBom -Path (Join-Path $LauncherDir 'install.ini') -Text $install
}

function Initialize-AccountLauncher {
    param([string]$AccountDir, [string]$AccountAppDir, [string]$Version)
    $localAppData = Join-Path $AccountDir 'localappdata'
    $launcherDir = Join-Path $localAppData 'Qoder CN\Qoder CN Launcher'
    New-Item -ItemType Directory -Path $launcherDir -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $launcherDir 'state.ini.writers') -Force | Out-Null

    $launcherSource = $RealLauncherExe
    if (-not (Test-Path -LiteralPath $launcherSource)) {
        $launcherSource = Join-Path $AccountAppDir 'resources\launcher\Qoder CN Launcher.exe'
    }
    if (-not (Test-Path -LiteralPath $launcherSource)) {
        throw 'Qoder CN Launcher.exe was not found.'
    }
    Link-OrCopyFile -From $launcherSource -To (Join-Path $launcherDir 'Qoder CN Launcher.exe') | Out-Null

    $i18nSource = Join-Path (Split-Path -Parent $launcherSource) 'i18n'
    if (Test-Path -LiteralPath $i18nSource) {
        $i18nTarget = Join-Path $launcherDir 'i18n'
        New-Item -ItemType Directory -Path $i18nTarget -Force | Out-Null
        foreach ($file in Get-ChildItem -LiteralPath $i18nSource -File -Force) {
            Link-OrCopyFile -From $file.FullName -To (Join-Path $i18nTarget $file.Name) | Out-Null
        }
    }

    Write-LauncherIniFiles -LauncherDir $launcherDir -InstallDir $AccountAppDir -Version $Version
    return $localAppData
}

function Write-AccountMeta {
    param([string]$Id, [string]$AccountDir, [string]$AccountAppDir, [string]$LocalAppData, [string]$DataDirectoryName, [string]$SourceInstall, [string]$Version)
    $meta = [ordered]@{
        schemaVersion     = 1
        id                = $Id
        appDir            = $AccountAppDir
        localAppData      = $LocalAppData
        dataDirectoryName = $DataDirectoryName
        sourceInstall     = $SourceInstall
        sourceVersion     = $Version
        createdAt         = (Get-Date).ToString('o')
    }
    $json = $meta | ConvertTo-Json -Depth 5
    Write-Utf8NoBom -Path (Join-Path $AccountDir 'account.json') -Text ($json + "`r`n")
}

function New-AccountShortcut {
    param([string]$Id, [string]$AccountAppDir)
    $desktop = [Environment]::GetFolderPath('Desktop')
    $programs = [Environment]::GetFolderPath('Programs')
    $targets = @($desktop)
    if ($programs) {
        $menuDir = Join-Path $programs 'Qoder CN Accounts'
        New-Item -ItemType Directory -Path $menuDir -Force | Out-Null
        $targets += $menuDir
    }
    $shell = New-Object -ComObject WScript.Shell
    $lnkName = "Qoder CN ($Id).lnk"
    $arguments = '"{0}" "{1}"' -f $LaunchVbs, $Id
    foreach ($dir in $targets) {
        $path = Join-Path $dir $lnkName
        $lnk = $shell.CreateShortcut($path)
        $lnk.TargetPath = $Wscript
        $lnk.Arguments = $arguments
        $lnk.WorkingDirectory = $AccountAppDir
        $lnk.IconLocation = (Join-Path $AccountAppDir 'Qoder CN.exe') + ',0'
        $lnk.Description = "Qoder CN isolated account: $Id"
        $lnk.Save()
    }
    [Runtime.InteropServices.Marshal]::ReleaseComObject($shell) | Out-Null
    return $lnkName
}

function Remove-AccountShortcut {
    param([string]$Id)
    $lnkName = "Qoder CN ($Id).lnk"
    $desktop = [Environment]::GetFolderPath('Desktop')
    $programs = [Environment]::GetFolderPath('Programs')
    foreach ($dir in @($desktop, (Join-Path $programs 'Qoder CN Accounts'))) {
        $p = Join-Path $dir $lnkName
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force }
    }
}

function Get-AccountProcessIds {
    param($Meta)
    $root = [IO.Path]::GetFullPath([string]$Meta.appDir).TrimEnd('\')
    $prefix = $root + '\'
    $list = @()
    foreach ($p in Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) {
        $exe = $p.ExecutablePath
        if ($exe -and $exe.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            $list += $p.ProcessId
        }
    }
    return ($list | Select-Object -Unique)
}

function Stop-AccountProcesses {
    param($Meta)
    $killed = 0
    for ($round = 1; $round -le 12; $round++) {
        $pids = @(Get-AccountProcessIds -Meta $Meta)
        if ($pids.Count -eq 0) { break }
        foreach ($pidValue in $pids) {
            try { & $Taskkill /PID $pidValue /T /F *> $null; $killed++ } catch { }
        }
        Start-Sleep -Milliseconds 600
    }
    $left = @(Get-AccountProcessIds -Meta $Meta)
    if ($left.Count -gt 0) {
        throw "Could not stop account processes (PID: $($left -join ', ')). Close the account window and try again."
    }
    return $killed
}

function Remove-PathWithRetry {
    param([string]$Path, [int]$Attempts = 8)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return
        } catch {
            if ($i -eq $Attempts) { throw "Could not delete $Path. Close all Qoder CN windows and try again. $($_.Exception.Message)" }
            Start-Sleep -Milliseconds 800
        }
    }
}

function Get-ProtocolCommand {
    $item = Get-ItemProperty -Path $ProtocolCommandKey -ErrorAction SilentlyContinue
    if (-not $item) { return $null }
    return [string]$item.'(default)'
}

function Ensure-ProtocolKey {
    if (-not (Test-Path -LiteralPath $ProtocolRoot)) {
        New-Item -Path $ProtocolRoot -Force | Out-Null
    }
    $key = Get-Item -LiteralPath $ProtocolRoot
    if ($null -eq $key.GetValue('URL Protocol', $null)) {
        New-ItemProperty -Path $ProtocolRoot -Name 'URL Protocol' -Value '' -PropertyType String | Out-Null
    }
    if (-not (Test-Path -LiteralPath $ProtocolCommandKey)) {
        New-Item -Path $ProtocolCommandKey -Force | Out-Null
    }
}

function Get-AccountProtocolCommand {
    param($Meta)
    return ('"{0}" "{1}" "{2}" "{3}" "%1"' -f $Wscript, $ProtocolVbs, $Meta.appDir, $Meta.localAppData)
}

function Set-AccountProtocol {
    param($Meta)
    if (-not (Test-Path -LiteralPath $ProtocolVbs)) { throw "Protocol shim missing: $ProtocolVbs" }
    Ensure-ProtocolKey
    Set-ItemProperty -Path $ProtocolCommandKey -Name '(default)' -Value (Get-AccountProtocolCommand -Meta $Meta)
}

function Reset-PrimaryProtocol {
    if (-not (Test-Path -LiteralPath $RealLauncherExe)) {
        throw "Official launcher not found: $RealLauncherExe"
    }
    Ensure-ProtocolKey
    $cmd = '"{0}" "%1"' -f $RealLauncherExe
    Set-ItemProperty -Path $ProtocolCommandKey -Name '(default)' -Value $cmd
    Write-Host "Protocol qoder-cn:// now opens the primary Qoder CN launcher."
}

function Test-ProtocolOwnedByAccount {
    param($Meta)
    $cmd = Get-ProtocolCommand
    if (-not $cmd) { return $false }
    $a = $cmd.IndexOf([string]$Meta.appDir, [StringComparison]::OrdinalIgnoreCase)
    $b = $cmd.IndexOf([string]$Meta.localAppData, [StringComparison]::OrdinalIgnoreCase)
    return (($a -ge 0) -or ($b -ge 0))
}

function Add-Account {
    if (-not $Name) { throw 'Account name is required. Example: add work' }
    $id = Normalize-AccountId $Name
    $accountDir = Get-AccountDir $id
    if (Test-Path -LiteralPath $accountDir) {
        throw "Account '$id' already exists. Remove it first or choose another name."
    }

    $primary = Get-PrimaryInstall
    $version = Get-InstallVersion -InstallDir $primary
    $product = Get-ProductConfig -InstallDir $primary
    if (-not $product.dataDirectoryName) {
        throw 'Primary product.json has no dataDirectoryName.'
    }
    $dataDirectoryName = "$($product.dataDirectoryName).$id"
    $accountAppDir = Join-Path $accountDir 'app'
    $dataDir = Join-Path ([Environment]::GetFolderPath('ApplicationData')) $dataDirectoryName

    Write-Host "Primary install : $primary"
    Write-Host "Account         : $id"
    Write-Host "Account data    : $dataDir"
    Write-Host 'Cloning installation (hardlinks where possible)...'

    try {
        New-Item -ItemType Directory -Path $accountAppDir -Force | Out-Null
        $stats = Copy-InstallTree -From $primary -To $accountAppDir
        Write-AccountProductJson -SourceInstall $primary -AccountAppDir $accountAppDir -Id $id -DataDirectoryName $dataDirectoryName
        $localAppData = Initialize-AccountLauncher -AccountDir $accountDir -AccountAppDir $accountAppDir -Version $version
        Write-AccountMeta -Id $id -AccountDir $accountDir -AccountAppDir $accountAppDir -LocalAppData $localAppData -DataDirectoryName $dataDirectoryName -SourceInstall $primary -Version $version
        $shortcut = $null
        if (-not $NoShortcut) {
            $shortcut = New-AccountShortcut -Id $id -AccountAppDir $accountAppDir
        }
    } catch {
        try {
            if (Test-Path -LiteralPath $accountDir) {
                [void](Move-ToTrash -Path $accountDir -Label "$id-add-failed")
            }
        } catch { }
        throw
    }

    Write-Host ("Cloned: {0} hardlinks ({1} MB shared), {2} private copies ({3} MB on disk)." -f $stats.Linked, $stats.LinkedMB, $stats.Copied, $stats.CopiedMB)
    if (Test-Path -LiteralPath $dataDir) {
        Write-Host "Existing isolated data found and will be reused: $dataDir"
    }
    if ($shortcut) {
        Write-Host "Shortcut created: $shortcut (Desktop and Start Menu)"
    }
    Write-Host ''
    Write-Host "Start it with:  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" launch $id"
    Write-Host 'On first launch, finish onboarding/login for this account. It can run at the same time as other accounts.'
    Write-Host 'The last launched account owns qoder-cn:// links. Use -NoProtocol to skip that behavior.'
}

function Launch-Account {
    if (-not $Name) { throw 'Account name is required. Example: launch work' }
    $id = Normalize-AccountId $Name
    $meta = Get-AccountMeta $id
    $exe = Join-Path $meta.appDir 'Qoder CN.exe'
    if (-not (Test-Path -LiteralPath $exe)) {
        throw "Account installation is incomplete: $exe. Run: sync $id -Force"
    }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.WorkingDirectory = $meta.appDir
    $psi.UseShellExecute = $false
    $psi.EnvironmentVariables['LOCALAPPDATA'] = [string]$meta.localAppData
    [System.Diagnostics.Process]::Start($psi) | Out-Null
    Write-Host "Started account '$id'."

    if ($NoProtocol) { return }

    try {
        Write-Host 'Routing qoder-cn:// to this account for the next 15 seconds...'
        $deadline = (Get-Date).AddSeconds(15)
        do {
            try {
                if ((Get-ProtocolCommand) -ne (Get-AccountProtocolCommand -Meta $meta)) {
                    Set-AccountProtocol -Meta $meta
                }
            } catch { }
            Start-Sleep -Milliseconds 700
        } while ((Get-Date) -lt $deadline)
        Set-AccountProtocol -Meta $meta
        Write-Host "Protocol handler now opens account '$id'."
    } catch {
        Write-Warning "Could not update the qoder-cn protocol handler: $($_.Exception.Message)"
    }
}

function Show-Accounts {
    Write-Host "Accounts root: $AccountsRoot"
    $primary = $null
    try {
        $primary = Get-PrimaryInstall
        Write-Host ("Primary install: {0} (version {1})" -f $primary, (Get-InstallVersion -InstallDir $primary))
    } catch {
        Write-Host 'Primary install: not found'
    }
    if (-not (Test-Path -LiteralPath $AccountsRoot)) {
        Write-Host 'No accounts yet. Create one with: add <name>'
        return
    }

    $rows = @()
    foreach ($dir in Get-ChildItem -LiteralPath $AccountsRoot -Directory -Force) {
        $metaPath = Join-Path $dir.FullName 'account.json'
        if (-not (Test-Path -LiteralPath $metaPath)) { continue }
        try {
            $meta = Get-Content -Raw -LiteralPath $metaPath | ConvertFrom-Json
            $running = @(Get-AccountProcessIds -Meta $meta).Count
            $dataDir = Join-Path ([Environment]::GetFolderPath('ApplicationData')) $meta.dataDirectoryName
            $shortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) ("Qoder CN ({0}).lnk" -f $meta.id)
            $rows += [pscustomobject]@{
                Account  = $meta.id
                Version  = $meta.sourceVersion
                Running  = $running
                Data     = (Test-Path -LiteralPath $dataDir)
                Shortcut = (Test-Path -LiteralPath $shortcut)
            }
        } catch {
            $rows += [pscustomobject]@{ Account = $dir.Name; Version = '?'; Running = '?'; Data = '?'; Shortcut = '?' }
        }
    }

    if ($rows.Count -eq 0) {
        Write-Host 'No accounts yet. Create one with: add <name>'
        return
    }
    $rows | Format-Table -AutoSize | Out-String | Write-Host
}

function Remove-Account {
    if (-not $Name) { throw 'Account name is required. Example: remove work' }
    $id = Normalize-AccountId $Name
    $meta = Get-AccountMeta $id
    $running = @(Get-AccountProcessIds -Meta $meta)
    if ($running.Count -gt 0) {
        if (-not $Force) {
            throw "Account '$id' is running (PID: $($running -join ', ')). Close it first, or use -Force."
        }
        Write-Host "Stopping account '$id' (PID: $($running -join ', '))..."
        Stop-AccountProcesses -Meta $meta | Out-Null
    }

    Write-Host "Removing account '$id'..."
    $moved = @()
    try {
        if (Test-Path -LiteralPath ([string]$meta.appDir)) {
            $dest = Move-ToTrash -Path ([string]$meta.appDir) -Label "$id-app"
            if ($dest) { $moved += [pscustomobject]@{ Source = [string]$meta.appDir; Trash = $dest } }
        }
        if (Test-Path -LiteralPath ([string]$meta.localAppData)) {
            $dest = Move-ToTrash -Path ([string]$meta.localAppData) -Label "$id-localappdata"
            if ($dest) { $moved += [pscustomobject]@{ Source = [string]$meta.localAppData; Trash = $dest } }
        }
    } catch {
        for ($i = $moved.Count - 1; $i -ge 0; $i--) {
            try {
                if ((Test-Path -LiteralPath $moved[$i].Trash) -and -not (Test-Path -LiteralPath $moved[$i].Source)) {
                    Move-Item -LiteralPath $moved[$i].Trash -Destination $moved[$i].Source -Force -ErrorAction Stop
                }
            } catch { }
        }
        throw
    }

    Remove-AccountShortcut -Id $id
    if (Test-ProtocolOwnedByAccount -Meta $meta) {
        try { Reset-PrimaryProtocol } catch { Write-Warning $_.Exception.Message }
    }

    foreach ($item in $moved) {
        [void](Remove-TreeBestEffort -Path $item.Trash)
    }

    if ($PurgeData) {
        $dataDir = Get-AccountDataDir -Meta $meta
        $appDataRoot = [IO.Path]::GetFullPath([Environment]::GetFolderPath('ApplicationData')).TrimEnd('\')
        $fullData = [IO.Path]::GetFullPath($dataDir)
        $leaf = Split-Path -Leaf $fullData
        if (-not $fullData.StartsWith($appDataRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or $leaf -notlike 'com.qodercn.app*') {
            throw "Refusing to delete unexpected data path: $fullData"
        }
        Write-Host "Purging account data: $fullData"
        try {
            Remove-PathWithRetry -Path $fullData -Attempts 4
        } catch {
            Write-Warning "Account removed, but its data could not be fully deleted: $fullData"
        }
    } else {
        Write-Host "Account data kept at: $(Get-AccountDataDir -Meta $meta) (use -PurgeData to delete)"
    }

    Remove-PathWithRetry -Path ([string]$meta.accountDir) -Attempts 3
    Clear-Trash
    $trash = Get-TrashInfo
    if ($trash.Files -gt 0) {
        Write-Host ("{0} hardlinked files remain in {1}; they use no extra disk and will be cleaned by 'cleanup' after Qoder CN closes." -f $trash.Files, $TrashRoot)
    }
    Write-Host "Account '$id' removed."
}

function Sync-Account {
    if (-not $Name) { throw 'Account name is required. Example: sync work' }
    $id = Normalize-AccountId $Name
    $meta = Get-AccountMeta $id
    $running = @(Get-AccountProcessIds -Meta $meta)
    if ($running.Count -gt 0) {
        if (-not $Force) {
            throw "Account '$id' is running (PID: $($running -join ', ')). Close it first, or use -Force."
        }
        Write-Host "Stopping account '$id'..."
        Stop-AccountProcesses -Meta $meta | Out-Null
    }

    $primary = Get-PrimaryInstall
    $version = Get-InstallVersion -InstallDir $primary
    $accountDir = [string]$meta.accountDir
    $accountAppDir = [string]$meta.appDir
    $dataDirectoryName = [string]$meta.dataDirectoryName
    $newApp = Join-Path $accountDir 'app.new'

    if (Test-Path -LiteralPath $newApp) {
        [void](Remove-TreeBestEffort -Path $newApp)
    }
    if (Test-Path -LiteralPath $newApp) {
        throw "Could not clear the previous staging directory: $newApp"
    }

    Write-Host "Refreshing account '$id' from: $primary (version $version)"
    New-Item -ItemType Directory -Path $newApp -Force | Out-Null
    try {
        $stats = Copy-InstallTree -From $primary -To $newApp
        Write-AccountProductJson -SourceInstall $primary -AccountAppDir $newApp -Id $id -DataDirectoryName $dataDirectoryName
    } catch {
        try {
            if (Test-Path -LiteralPath $newApp) { [void](Move-ToTrash -Path $newApp -Label "$id-sync-failed") }
        } catch { }
        throw
    }

    $oldTrash = $null
    try {
        if (Test-Path -LiteralPath $accountAppDir) {
            $oldTrash = Move-ToTrash -Path $accountAppDir -Label "$id-app"
        }
        Rename-Item -LiteralPath $newApp -NewName 'app' -ErrorAction Stop
    } catch {
        if ($oldTrash -and -not (Test-Path -LiteralPath $accountAppDir)) {
            try { Move-Item -LiteralPath $oldTrash -Destination $accountAppDir -Force -ErrorAction Stop; $oldTrash = $null } catch { }
        }
        try {
            if (Test-Path -LiteralPath $newApp) { [void](Move-ToTrash -Path $newApp -Label "$id-sync-failed") }
        } catch { }
        throw
    }

    try {
        $localAppData = Initialize-AccountLauncher -AccountDir $accountDir -AccountAppDir $accountAppDir -Version $version
        Write-AccountMeta -Id $id -AccountDir $accountDir -AccountAppDir $accountAppDir -LocalAppData $localAppData -DataDirectoryName $dataDirectoryName -SourceInstall $primary -Version $version
    } catch {
        $failedApp = $null
        try {
            if (Test-Path -LiteralPath $accountAppDir) { $failedApp = Move-ToTrash -Path $accountAppDir -Label "$id-sync-failed" }
            if ($oldTrash -and -not (Test-Path -LiteralPath $accountAppDir)) {
                Move-Item -LiteralPath $oldTrash -Destination $accountAppDir -Force -ErrorAction Stop
                $oldTrash = $null
            }
        } catch { }
        if ($failedApp) { [void](Remove-TreeBestEffort -Path $failedApp) }
        throw
    }

    if ($oldTrash) {
        [void](Remove-TreeBestEffort -Path $oldTrash)
    }
    Clear-Trash
    $trash = Get-TrashInfo
    Write-Host ("Synced: {0} hardlinks ({1} MB shared), {2} private copies ({3} MB on disk). Account data was not touched." -f $stats.Linked, $stats.LinkedMB, $stats.Copied, $stats.CopiedMB)
    if ($trash.Files -gt 0) {
        Write-Host ("{0} hardlinked files remain in {1}; they use no extra disk and will be cleaned by 'cleanup' after Qoder CN closes." -f $trash.Files, $TrashRoot)
    }
}

function Show-TrashStatus {
    Clear-Trash
    $trash = Get-TrashInfo
    if ($trash.Files -eq 0) {
        Write-Host 'No leftover files.'
        return
    }
    Write-Host ("{0} hardlinked files remain in {1}." -f $trash.Files, $TrashRoot)
    Write-Host 'They are kept alive by running Qoder CN instances. They use no extra disk and will be removed after all Qoder CN windows are closed.'
}

function Show-Help {
    Write-Host @"
Qoder CN multi-account manager (Windows)

Usage:
  powershell -ExecutionPolicy Bypass -File QoderCN-Multi.ps1 add <name> [-Source <installDir>] [-NoShortcut]
  powershell -ExecutionPolicy Bypass -File QoderCN-Multi.ps1 list
  powershell -ExecutionPolicy Bypass -File QoderCN-Multi.ps1 launch <name> [-NoProtocol]
  powershell -ExecutionPolicy Bypass -File QoderCN-Multi.ps1 sync <name> [-Force]
  powershell -ExecutionPolicy Bypass -File QoderCN-Multi.ps1 remove <name> [-Force] [-PurgeData]
  powershell -ExecutionPolicy Bypass -File QoderCN-Multi.ps1 cleanup
  powershell -ExecutionPolicy Bypass -File QoderCN-Multi.ps1 reset-protocol

How it works:
  - Each account gets its own install directory with a unique product.json
    dataDirectoryName and launcher state under an isolated LOCALAPPDATA. That
    gives each account a separate login, Electron profile, single-instance lock
    and GUI.
  - Install files are hardlinked to save disk. Files that a running Qoder CN
    has memory-mapped are copied per account so sync/remove work while the
    primary app stays open.
  - Accounts live in: $AccountsRoot
  - Account data lives in: %APPDATA%\com.qodercn.app.stable.<name>
  - Desktop/Start Menu shortcuts start an account silently via wscript.exe.

Notes:
  - The last launched account owns qoder-cn:// links so browser login callbacks
    reach the right instance. Launch the primary shortcut to give links back to
    the primary account, or run reset-protocol.
  - account names: 1-32 chars, letters/digits/_/-, lowercase is used.
  - After the official Qoder CN app updates, run: sync <name> -Force for each
    account to refresh binaries (login data is preserved).
  - Removing an account keeps its data unless -PurgeData is used.
  - Hardlinked files still mapped by a running Qoder CN are parked in
    $TrashRoot and cleaned automatically once Qoder CN is closed.
"@
}

try {
    if ($Command -in @('list', 'add', 'sync', 'remove', 'reset-protocol')) {
        try { Clear-Trash } catch { }
    }
    switch ($Command) {
        'add' { Add-Account }
        'list' { Show-Accounts }
        'launch' { Launch-Account }
        'remove' { Remove-Account }
        'sync' { Sync-Account }
        'cleanup' { Show-TrashStatus }
        'reset-protocol' { Reset-PrimaryProtocol }
        default { Show-Help }
    }
} catch {
    Write-Error $_.Exception.Message
}
