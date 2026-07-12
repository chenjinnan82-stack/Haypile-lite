[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "Haypile Windows builds must run on Windows."
}
if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne "X64") {
    throw "Haypile v0.2 Windows builds require x64 Windows."
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Venv = Join-Path $Root ".build-venv"
$VenvPython = Join-Path $Venv "Scripts/python.exe"
$DeployTool = Join-Path $Venv "Scripts/pyside6-deploy.exe"
$BuildDir = Join-Path $Root "build"
$WindowsDeployDir = Join-Path $BuildDir "windows-deploy"
$DistDir = Join-Path $Root "dist"
$PortableDir = Join-Path $DistDir "Haypile"
$Zip = Join-Path $DistDir "Haypile-v0.2.0-windows-x64.zip"
$Checksum = "$Zip.sha256"
$IconSource = Join-Path $Root "assets/haypile-app-icon.png"
$Icon = Join-Path $BuildDir "Haypile.ico"
$Spec = Join-Path $Root "pysidedeploy.windows.spec"
$SpecBackup = [System.IO.Path]::GetTempFileName()
$SmokeRoot = Join-Path $env:TEMP "haypile-windows-smoke-$PID"
$SmokePort = if ($env:HAYPILE_SMOKE_PORT) { [int]$env:HAYPILE_SMOKE_PORT } else { 18010 }
$BackendProcess = $null

function Assert-LastExitCode([string]$Message) {
    if ($LASTEXITCODE -ne 0) {
        throw $Message
    }
}

function Test-TcpPort([int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        return $task.Wait(250) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

Push-Location $Root
Copy-Item $Spec $SpecBackup -Force
try {
    if (-not (Test-Path $IconSource -PathType Leaf)) {
        throw "Missing Windows icon source: $IconSource"
    }
    if (-not (Test-Path $VenvPython -PathType Leaf)) {
        if ($SkipInstall) {
            throw "Missing build environment: $VenvPython"
        }
        & $Python -m venv $Venv
        Assert-LastExitCode "Failed to create the Windows build environment."
    }
    if (-not $SkipInstall) {
        & $VenvPython -m pip install --quiet --upgrade pip
        Assert-LastExitCode "Failed to upgrade pip."
        & $VenvPython -m pip install --quiet -r requirements-desktop.txt "Nuitka==4.0"
        Assert-LastExitCode "Failed to install Windows build dependencies."
    }

    Remove-Item $WindowsDeployDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $PortableDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $Zip -Force -ErrorAction SilentlyContinue
    Remove-Item $Checksum -Force -ErrorAction SilentlyContinue
    New-Item $BuildDir -ItemType Directory -Force | Out-Null
    New-Item $DistDir -ItemType Directory -Force | Out-Null

    $IconCode = "from PIL import Image; import sys; image=Image.open(sys.argv[1]).convert('RGBA'); image.save(sys.argv[2], format='ICO', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
    & $VenvPython -c $IconCode $IconSource $Icon
    Assert-LastExitCode "Failed to generate Haypile.ico."

    & $DeployTool -c $Spec -f
    Assert-LastExitCode "pyside6-deploy failed."

    $BuiltExe = Get-ChildItem $WindowsDeployDir -Filter "Haypile.exe" -File -Recurse | Select-Object -First 1
    if ($null -eq $BuiltExe) {
        throw "Haypile.exe was not produced."
    }
    New-Item $PortableDir -ItemType Directory -Force | Out-Null
    Copy-Item (Join-Path $BuiltExe.Directory.FullName "*") $PortableDir -Recurse -Force
    $Exe = Join-Path $PortableDir "Haypile.exe"

    foreach ($RequiredPath in @(
        $Exe,
        (Join-Path $PortableDir "ui_assets/haypile-icon.png"),
        (Join-Path $PortableDir "ui_assets/drop-leaf-frame.svg"),
        (Join-Path $PortableDir "assets/haypile-app-icon.png")
    )) {
        if (-not (Test-Path $RequiredPath -PathType Leaf)) {
            throw "Missing packaged resource: $RequiredPath"
        }
    }
    if (-not (Get-ChildItem $PortableDir -Filter "qwindows.dll" -File -Recurse | Select-Object -First 1)) {
        throw "Missing Qt Windows platform plugin qwindows.dll."
    }

    $McpInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $McpInfo.FileName = $Exe
    $McpInfo.Arguments = "--mcp"
    $McpInfo.UseShellExecute = $false
    $McpInfo.CreateNoWindow = $true
    $McpInfo.RedirectStandardInput = $true
    $McpInfo.RedirectStandardOutput = $true
    $McpInfo.RedirectStandardError = $true
    $McpProcess = [System.Diagnostics.Process]::new()
    $McpProcess.StartInfo = $McpInfo
    [void]$McpProcess.Start()
    $McpOutputTask = $McpProcess.StandardOutput.ReadToEndAsync()
    $McpErrorTask = $McpProcess.StandardError.ReadToEndAsync()
    $McpProcess.StandardInput.WriteLine('{"jsonrpc":"2.0","id":1,"method":"initialize"}')
    $McpProcess.StandardInput.WriteLine('{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
    $McpProcess.StandardInput.Close()
    if (-not $McpProcess.WaitForExit(10000)) {
        $McpProcess.Kill($true)
        throw "Haypile.exe --mcp did not exit after stdin closed."
    }
    $McpOutput = $McpOutputTask.Result
    $McpError = $McpErrorTask.Result
    if ($McpProcess.ExitCode -ne 0) {
        throw "Haypile.exe --mcp failed: $McpError"
    }
    if ($McpOutput -notmatch '"version"\s*:\s*"0\.2\.0"') {
        throw "Haypile.exe --mcp did not return server version 0.2.0."
    }

    New-Item $SmokeRoot -ItemType Directory -Force | Out-Null
    $BackendInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $BackendInfo.FileName = $Exe
    $BackendInfo.Arguments = "--backend"
    $BackendInfo.UseShellExecute = $false
    $BackendInfo.CreateNoWindow = $true
    $BackendInfo.Environment["STORAGE_DIR"] = (Join-Path $SmokeRoot "storage")
    $BackendInfo.Environment["PORT"] = [string]$SmokePort
    $BackendInfo.Environment["IPC_CHANNEL"] = "haypile_v020_windows_smoke_$PID"
    $BackendInfo.Environment["HAYPILE_IPC_AUTHKEY_FILE"] = (Join-Path $SmokeRoot "ipc_authkey")
    $BackendProcess = [System.Diagnostics.Process]::Start($BackendInfo)

    $BackendReady = $false
    for ($Attempt = 0; $Attempt -lt 50; $Attempt++) {
        try {
            $Health = Invoke-RestMethod "http://127.0.0.1:$SmokePort/healthz" -TimeoutSec 1
            if ($Health.status -eq "ok") {
                $BackendReady = $true
                break
            }
        }
        catch {
        }
        Start-Sleep -Milliseconds 100
    }
    if (-not $BackendReady) {
        throw "Haypile.exe --backend did not become ready."
    }
    Invoke-RestMethod "http://127.0.0.1:$SmokePort/readyz" -TimeoutSec 2 | Out-Null
    Invoke-RestMethod "http://127.0.0.1:$SmokePort/api/v1/bundles?status=ready" -TimeoutSec 2 | Out-Null

    $BackendProcess.Kill($true)
    [void]$BackendProcess.WaitForExit(5000)
    $BackendProcess = $null
    for ($Attempt = 0; $Attempt -lt 20 -and (Test-TcpPort $SmokePort); $Attempt++) {
        Start-Sleep -Milliseconds 100
    }
    if (Test-TcpPort $SmokePort) {
        throw "Haypile backend left port $SmokePort open."
    }

    Compress-Archive -Path $PortableDir -DestinationPath $Zip -CompressionLevel Optimal
    $Hash = (Get-FileHash $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
    $ChecksumLine = "$Hash  $([System.IO.Path]::GetFileName($Zip))$([Environment]::NewLine)"
    [System.IO.File]::WriteAllText($Checksum, $ChecksumLine, [System.Text.Encoding]::ASCII)

    Write-Host "Built: $PortableDir"
    Write-Host "Archive: $Zip"
    Write-Host "Checksum: $Checksum"
}
finally {
    if ($null -ne $BackendProcess -and -not $BackendProcess.HasExited) {
        $BackendProcess.Kill($true)
        [void]$BackendProcess.WaitForExit(5000)
    }
    Remove-Item $SmokeRoot -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item $SpecBackup $Spec -Force
    Remove-Item $SpecBackup -Force -ErrorAction SilentlyContinue
    Pop-Location
}
