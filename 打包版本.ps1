param([string]$Version = "1.6.7", [string]$OutputDirectory = "dist")
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidates = @((Join-Path $env:LocalAppData "Programs\Python\Python313\python.exe"),
    (Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe"),
    (Join-Path $env:LocalAppData "Programs\Python\Python311\python.exe"))
$pythonPath = $null
foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        & $candidate -c "import PyInstaller" 2>$null
        if ($LASTEXITCODE -eq 0) { $pythonPath = $candidate; break }
    }
}
if ($null -eq $pythonPath) { throw "Python with PyInstaller is required." }
& $pythonPath -B (Join-Path $projectRoot "build_tools\build_release.py") --source-root $projectRoot --output-directory $OutputDirectory --version $Version
if ($LASTEXITCODE -ne 0) { throw "Package build failed." }
