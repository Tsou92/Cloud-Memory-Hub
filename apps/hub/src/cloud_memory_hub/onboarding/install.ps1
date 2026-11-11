param(
    [Parameter(Mandatory=$true)][string]$Link,
    [Parameter(Mandatory=$true)][ValidateSet('claude','codex','opencode','hermes','generic')][string]$Agent
)
$ErrorActionPreference = 'Stop'
$cmhOrigin = __CMH_ORIGIN_POWERSHELL__
$cmhScriptSha = '__CMH_CONNECT_SHA256__'
$cmhTemp = Join-Path ([IO.Path]::GetTempPath()) ('cmh-connect-' + [Guid]::NewGuid().ToString('N'))
$cmhFailure = $false
$cmhStage = 'component_download'
__CMH_HUB_POWERSHELL_FETCH__
try {
    New-Item -ItemType Directory -Path $cmhTemp | Out-Null
    $cmhScript = Join-Path $cmhTemp 'connect.py'
    Get-CmhHubFile -Uri ($cmhOrigin + '/hub/connect.py') -OutFile $cmhScript
    $cmhStage = 'checksum_read'
    $cmhActualSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $cmhScript).Hash.ToLowerInvariant()
    $cmhStage = 'checksum_compare'
    if ($cmhActualSha -ne $cmhScriptSha) {
        throw 'component_checksum_mismatch'
    }
    $cmhStage = 'python_probe'
    $cmhPython = $null
    foreach ($cmhCandidate in @('python3.12', 'python3', 'python')) {
        $cmhExecutable = Get-Command $cmhCandidate -ErrorAction SilentlyContinue
        if ($cmhExecutable -and $cmhExecutable.Source -notlike '*\WindowsApps\*') {
            $cmhProbePassed = $false
            $cmhProbePreference = $ErrorActionPreference
            try {
                # PS 5.1 turns native stderr into ErrorRecords; a failed candidate
                # must not prevent trying the next interpreter.
                $ErrorActionPreference = 'Continue'
                & $cmhExecutable.Source -I -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>$null
                $cmhProbePassed = $? -and $LASTEXITCODE -eq 0
            } catch { $cmhProbePassed = $false }
            finally { $ErrorActionPreference = $cmhProbePreference }
            if ($cmhProbePassed) { $cmhPython = $cmhExecutable.Source; break }
        }
    }
    if ($cmhPython) {
        $cmhStage = 'python_run'
        & $cmhPython -I $cmhScript --link $Link --agent $Agent
        $cmhExit = $LASTEXITCODE
    } else {
        $cmhStage = 'runtime_prepare'
        $cmhArch = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        switch ($cmhArch) {
            'X64' { $cmhTarget = 'x86_64-pc-windows-msvc'; $cmhUvSha = '4c4d49d8738847d9b71ba319e49a5688c93eac0fe6204b1df24e98528dddf39a' } # pragma: allowlist secret
            'Arm64' { $cmhTarget = 'aarch64-pc-windows-msvc'; $cmhUvSha = '724279317fee6e5fa8ad1908e4eba2bbe764ef1ece5b3f4597927b62b1fe562a' } # pragma: allowlist secret
            default { throw 'platform_unsupported' }
        }
        Write-Host '正在准备一次性连接环境，无需管理员权限。'
        $cmhArchive = Join-Path $cmhTemp 'uv.zip'
        Invoke-WebRequest -UseBasicParsing -Uri ('https://github.com/astral-sh/uv/releases/download/0.12.5/uv-' + $cmhTarget + '.zip') -OutFile $cmhArchive -TimeoutSec 180
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $cmhArchive).Hash.ToLowerInvariant() -ne $cmhUvSha) {
            throw 'runtime_checksum_mismatch'
        }
        Expand-Archive -LiteralPath $cmhArchive -DestinationPath (Join-Path $cmhTemp 'uv')
        $cmhUv = Join-Path $cmhTemp 'uv\uv.exe'
        # Process-scoped environment is restored even when preparation fails.
        $cmhOldCache = $env:UV_CACHE_DIR
        $cmhOldPython = $env:UV_PYTHON_INSTALL_DIR
        try {
            $env:UV_CACHE_DIR = Join-Path $cmhTemp 'cache'
            $env:UV_PYTHON_INSTALL_DIR = Join-Path $cmhTemp 'python'
            & $cmhUv run --no-project --no-config --no-env-file --isolated --managed-python --python 3.12.13 --no-progress python -I $cmhScript --link $Link --agent $Agent
            $cmhExit = $LASTEXITCODE
        } finally {
            $env:UV_CACHE_DIR = $cmhOldCache
            $env:UV_PYTHON_INSTALL_DIR = $cmhOldPython
        }
    }
    if ($cmhExit -ne 0) { throw 'connection_incomplete' }
} catch {
    # Never echo the exception, invocation, URL fragment or long-term credential.
    $cmhFailure = $true
    if ($cmhStage -in @('component_download','checksum_read','checksum_compare','python_probe','python_run','runtime_prepare')) {
        [Console]::Error.WriteLine('cmh_connect_stage=' + $cmhStage)
        [Console]::Error.WriteLine('cmh_error_type=' + $_.Exception.GetType().FullName)
    }
} finally {
    if (Test-Path -LiteralPath $cmhTemp) { Remove-Item -LiteralPath $cmhTemp -Recurse -Force }
}
if ($cmhFailure) {
    $global:LASTEXITCODE = 1
    throw [System.InvalidOperationException]::new('连接未完成。请按上方提示处理，已保存的配对进度会保留。')
}
$global:LASTEXITCODE = 0
