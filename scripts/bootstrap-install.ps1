param(
    [string]$Nas,
    [switch]$Local,
    [ValidateRange(1024,65535)][int]$Port,
    [ValidateRange(1,65535)][int]$SshPort,
    [string]$Origin,
    [string]$TailscaleContainer,
    [switch]$OpenAdmin,
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'
$cmhTag = 'v0.1.7'
$cmhRelease = "https://github.com/Tsou92/Cloud-Memory-Hub/releases/download/$cmhTag"
$cmhArchiveName = "cloud-memory-hub-$cmhTag-source.tar.gz"
$cmhTemp = Join-Path ([IO.Path]::GetTempPath()) ('cmh-release-' + [Guid]::NewGuid().ToString('N'))
$cmhIsWindows = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
if (-not $cmhIsWindows -and -not $VerifyOnly) {
    throw 'Mac 和 Linux 请使用对应的终端安装命令。'
}
$cmhCurlName = if ($cmhIsWindows) { 'curl.exe' } else { 'curl' }
$cmhCurl = Get-Command $cmhCurlName -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $cmhCurl) {
    throw '需要 Windows 自带的 curl.exe。请更新系统或安装 curl 后重新运行。'
}
if (-not $VerifyOnly) {
    if ($Local -and $Nas) { throw '请选择 -Local 或 -Nas，分别部署到本机或 NAS。' }
    if (-not $Local -and ([string]::IsNullOrWhiteSpace($Nas) -or $Nas -ceq 'USER@NAS')) {
        throw '请把 USER@NAS 换成 NAS 的 SSH 用户名和地址，或填写已有的 SSH 别名。'
    }
    if (-not $Local -and -not (Get-Command ssh.exe -CommandType Application -ErrorAction SilentlyContinue)) {
        throw '请在 Windows「设置 → 可选功能」安装 OpenSSH 客户端，然后重新运行同一条命令。'
    }
}

function Get-CmhReleaseFile {
    param([string]$Url, [string]$File, [long]$Maximum, [int]$Seconds)
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $status = 28
    $attempt = 0
    Write-Host ("正在下载 " + [IO.Path]::GetFileName($File) + "…")
    foreach ($route in @('environment', 'direct', 'environment', 'direct')) {
        $remaining = $Seconds - [int][Math]::Ceiling($watch.Elapsed.TotalSeconds)
        if ($remaining -lt 1) { break }
        $attempt += 1
        $curlArgs = @(
            '-q', '-fsL', '--http1.1', '--proto', '=https', '--proto-redir', '=https',
            '--max-redirs', '3', '--connect-timeout', '12', '--max-time', "$remaining",
            '--max-filesize', "$Maximum", '--speed-limit', '4096', '--speed-time', '30'
        )
        if ($route -eq 'direct') { $curlArgs += @('--noproxy', '*') }
        $preference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $cmhCurl.Source @curlArgs $Url -o $File 2>$null
            $status = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $preference
        }
        if ($status -eq 0) {
            $bytes = (Get-Item -LiteralPath $File).Length
            if ($bytes -lt 1 -or $bytes -gt $Maximum) { throw '发行附件大小不符合预期。' }
            return
        }
        if ($attempt -lt 4 -and $watch.Elapsed.TotalSeconds + 1 -lt $Seconds) {
            Write-Host ("下载中断（curl " + $status + "），正在重试。")
            Start-Sleep -Seconds 1
        }
    }
    throw ("下载 " + [IO.Path]::GetFileName($File) + " 失败（curl " + $status + "）。请检查 GitHub 网络后重试。")
}

function Get-CmhSha256 {
    param([string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function Get-CmhPython {
    foreach ($candidate in @('python3.12', 'python3', 'python', 'py')) {
        $command = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $command -or $command.Source -like '*\WindowsApps\*') { continue }
        $launcherArgs = @()
        if ($candidate -eq 'py') { $launcherArgs = @('-3') }
        $preference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $command.Source @launcherArgs -I -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>$null
            $passed = $LASTEXITCODE -eq 0
        } catch {
            $passed = $false
        } finally {
            $ErrorActionPreference = $preference
        }
        if ($passed) { return @($command.Source) + $launcherArgs }
    }
}

try {
    [IO.Directory]::CreateDirectory($cmhTemp) | Out-Null
    $checksums = Join-Path $cmhTemp 'SHA256SUMS'
    $archive = Join-Path $cmhTemp $cmhArchiveName
    Get-CmhReleaseFile "$cmhRelease/SHA256SUMS" $checksums 65536 60
    $pattern = '^([0-9a-f]{64})  ' + [regex]::Escape($cmhArchiveName) + '$'
    $matchesByName = @(
        Get-Content -LiteralPath $checksums | Where-Object {
            ($_ -split '\s+', 3)[1] -ceq $cmhArchiveName
        }
    )
    if ($matchesByName.Count -ne 1) {
        throw '官方校验清单缺少唯一有效的源码包校验值。'
    }
    $shaMatch = [regex]::Match($matchesByName[0], $pattern)
    if (-not $shaMatch.Success) { throw '官方校验清单中的源码包校验值无效。' }
    $expectedSha = $shaMatch.Groups[1].Value
    Get-CmhReleaseFile "$cmhRelease/$cmhArchiveName" $archive 134217728 180
    if ((Get-CmhSha256 $archive) -cne $expectedSha) {
        throw '源码包校验失败，安装已停止。'
    }
    Write-Host '发行源码包校验通过。'

    if ($VerifyOnly) {
        $installerArgs = @('--help')
    } else {
        $installerArgs = @('--tag', $cmhTag)
        if ($Local) { $installerArgs += '--local' }
        else { $installerArgs += @('--nas', $Nas) }
        if ($OpenAdmin) { $installerArgs += '--open-admin' }
        elseif (-not $Local) { $installerArgs += '--nas-only' }
        if ($PSBoundParameters.ContainsKey('Port')) { $installerArgs += @('--port', "$Port") }
        if ($PSBoundParameters.ContainsKey('SshPort')) { $installerArgs += @('--ssh-port', "$SshPort") }
        if ($Origin) { $installerArgs += @('--origin', $Origin) }
        if ($TailscaleContainer) { $installerArgs += @('--tailscale-container', $TailscaleContainer) }
    }
    # Never extract to paths supplied by the archive. Only copy the installer files.
    $runner = Join-Path $cmhTemp 'run-installer.py'
    Set-Content -LiteralPath $runner -Encoding ASCII -Value @'
from pathlib import Path
import subprocess
import sys
import tarfile

archive, tag, *arguments = sys.argv[1:]
root = Path(archive).parent / "source"
root.mkdir()
files = (
    "scripts/install_hub.py",
    "scripts/install_local_docker.py",
    "scripts/install_nas.sh",
    "scripts/install_release_fetch.py",
    "scripts/install_first_device.py",
    "scripts/admin_entry.py",
    "apps/hub/src/cloud_memory_hub/onboarding/connect.py",
)
members = {f"cloud-memory-hub-{tag}/{name}": name for name in files}
seen = set()
with tarfile.open(archive, mode="r|gz") as source:
    for member in source:
        name = members.get(member.name)
        if name is None:
            continue
        if name in seen or not member.isfile() or not 0 < member.size <= 2 * 1024 * 1024:
            raise SystemExit("cmh_release_member_invalid")
        seen.add(name)
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.extractfile(member) as stream:
            target.write_bytes(stream.read())
if seen != set(files):
    raise SystemExit("cmh_release_member_missing")
raise SystemExit(
    subprocess.run(
        [sys.executable, "-I", "-X", "utf8", str(root / files[0]), *arguments], check=False
    ).returncode
)
'@
    $python = @(Get-CmhPython)
    if ($python.Count -gt 0) {
        $pythonExe = $python[0]
        $pythonArgs = @($python | Select-Object -Skip 1)
        & $pythonExe @pythonArgs -I $runner $archive $cmhTag @installerArgs
        $installerExit = $LASTEXITCODE
    } else {
        if (-not $cmhIsWindows) { throw '验证安装器需要 Python 3.11 或更高版本。' }
        $architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        switch ($architecture) {
            'X64' { $uvTarget = 'x86_64-pc-windows-msvc'; $uvSha = '4c4d49d8738847d9b71ba319e49a5688c93eac0fe6204b1df24e98528dddf39a' } # pragma: allowlist secret
            'Arm64' { $uvTarget = 'aarch64-pc-windows-msvc'; $uvSha = '724279317fee6e5fa8ad1908e4eba2bbe764ef1ece5b3f4597927b62b1fe562a' } # pragma: allowlist secret
            default { throw '此 Windows 架构需要预先安装 Python 3.11 或更高版本。' }
        }
        Write-Host '正在准备安装运行环境，无需管理员权限。'
        $uvArchive = Join-Path $cmhTemp 'uv.zip'
        Get-CmhReleaseFile "https://github.com/astral-sh/uv/releases/download/0.12.5/uv-$uvTarget.zip" $uvArchive 134217728 180
        if ((Get-CmhSha256 $uvArchive) -cne $uvSha) {
            throw '运行环境校验失败，安装已停止。'
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $uv = Join-Path $cmhTemp 'uv.exe'
        $zip = [IO.Compression.ZipFile]::OpenRead($uvArchive)
        try {
            $entries = @($zip.Entries | Where-Object FullName -CEQ 'uv.exe')
            if ($entries.Count -ne 1 -or $entries[0].Length -le 0 -or $entries[0].Length -gt 134217728) {
                throw '运行环境文件缺失或大小不符合预期。'
            }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entries[0], $uv)
        } finally {
            $zip.Dispose()
        }
        $oldCache = $env:UV_CACHE_DIR
        $oldPython = $env:UV_PYTHON_INSTALL_DIR
        try {
            $env:UV_CACHE_DIR = Join-Path $cmhTemp 'cache'
            $env:UV_PYTHON_INSTALL_DIR = Join-Path $cmhTemp 'python'
            & $uv run --no-project --no-config --no-env-file --isolated --managed-python --python 3.12.13 --no-progress python -I $runner $archive $cmhTag @installerArgs
            $installerExit = $LASTEXITCODE
        } finally {
            $env:UV_CACHE_DIR = $oldCache
            $env:UV_PYTHON_INSTALL_DIR = $oldPython
        }
    }
    $global:LASTEXITCODE = $installerExit
    if ($installerExit -ne 0) { throw '安装未完成。请按上面的具体提示处理。' }
} finally {
    if (Test-Path -LiteralPath $cmhTemp) { Remove-Item -LiteralPath $cmhTemp -Recurse -Force }
}
