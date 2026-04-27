<#
.SYNOPSIS
  Build a Windows Blender executable for CodeTether automation.

.DESCRIPTION
  Intended to run inside the temporary Windows VM created by Google Cloud
  Build's windows-builder. It prepares Blender's Windows dependencies, configures
  a Ninja/MSVC build that keeps UI/editor functionality enabled, packages the
  result, and optionally uploads artifacts to GCS.
#>

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Invoke-Logged {
  param(
    [Parameter(Mandatory=$true)][string]$FilePath,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments
  )
  Write-Host "> $FilePath $($Arguments -join ' ')"
  & $FilePath @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "Command failed with exit code $LASTEXITCODE: $FilePath $($Arguments -join ' ')"
  }
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$BuildDir = Join-Path (Split-Path $RepoRoot -Parent) 'build_windows_codetether'
$InstallDir = Join-Path $BuildDir 'install'
$PackageDir = Join-Path $RepoRoot 'artifacts'
$PackageName = if ($env:CODETETHER_PACKAGE_NAME) { $env:CODETETHER_PACKAGE_NAME } else { 'blender-codetether-windows-x64' }
$ArtifactBucket = $env:ARTIFACT_BUCKET

Write-Host "Repository: $RepoRoot"
Write-Host "Build dir:  $BuildDir"
Write-Host "Install:    $InstallDir"

Set-Location $RepoRoot

# Download/update the prebuilt Windows dependency bundle used by Blender builds.
Invoke-Logged '.\make.bat' 'update'

if (Test-Path $BuildDir) {
  Remove-Item -Recurse -Force $BuildDir
}
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
New-Item -ItemType Directory -Force -Path $PackageDir | Out-Null

$VsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (!(Test-Path $VsWhere)) {
  throw 'Visual Studio Build Tools are required on the Windows builder VM, but vswhere.exe was not found.'
}
$VsInstall = & $VsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (!$VsInstall) {
  throw 'No Visual Studio installation with MSVC x64 tools was found.'
}
$VcVars = Join-Path $VsInstall 'VC\Auxiliary\Build\vcvars64.bat'
if (!(Test-Path $VcVars)) {
  throw "vcvars64.bat not found at $VcVars"
}

$ConfigureAndBuild = @"
call `"$VcVars`"
cd /d `"$RepoRoot`"
cmake -S `"$RepoRoot`" -B `"$BuildDir`" -G Ninja -C `"$RepoRoot\build_files\cmake\config\blender_codetether_windows.cmake`" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=`"$InstallDir`"
if errorlevel 1 exit /b %errorlevel%
cmake --build `"$BuildDir`" --config Release --target install --parallel %NUMBER_OF_PROCESSORS%
if errorlevel 1 exit /b %errorlevel%
"@

$CmdFile = Join-Path $BuildDir 'configure_and_build.cmd'
Set-Content -Path $CmdFile -Value $ConfigureAndBuild -Encoding ASCII
Invoke-Logged 'cmd.exe' '/c' $CmdFile

$Exe = Get-ChildItem -Path $InstallDir -Recurse -Filter 'blender.exe' | Select-Object -First 1
if (!$Exe) {
  throw "Build completed, but blender.exe was not found under $InstallDir"
}
Write-Host "Found executable: $($Exe.FullName)"

$VersionText = & $Exe.FullName --version 2>&1 | Select-Object -First 5
$VersionText | Tee-Object -FilePath (Join-Path $PackageDir 'blender-version.txt')

$ZipPath = Join-Path $PackageDir "$PackageName.zip"
if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
Compress-Archive -Path (Join-Path $InstallDir '*') -DestinationPath $ZipPath -CompressionLevel Optimal
Write-Host "Packaged $ZipPath"

if ($ArtifactBucket) {
  Write-Host "Uploading artifacts to gs://$ArtifactBucket/"
  Invoke-Logged 'gcloud.cmd' 'storage' 'cp' $ZipPath "gs://$ArtifactBucket/$PackageName.zip"
  Invoke-Logged 'gcloud.cmd' 'storage' 'cp' (Join-Path $PackageDir 'blender-version.txt') "gs://$ArtifactBucket/blender-version.txt"
}
else {
  Write-Host 'ARTIFACT_BUCKET is not set; artifacts remain in the Cloud Build workspace artifacts directory.'
}
