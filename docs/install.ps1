# xresume installer for Windows.
#
#   irm https://eekebin.github.io/xresume/install.ps1 | iex
#
# Puts xresume.exe in %LOCALAPPDATA%\xresume\bin and adds that folder to your user PATH.
# Run it again to update.
#
# It installs the standalone xresume.exe, which needs nothing else. If that cannot be downloaded
# or does not run here, it builds the Python version instead, which needs Python 3.8 or newer and
# gets a private Python environment of its own.
#
#   $env:XRESUME_HOME_DIR   folder to install into (default %LOCALAPPDATA%\xresume)
#   $env:XRESUME_NO_BINARY  set to 1 to install the Python version
#   $env:XRESUME_RELEASES   where to fetch xresume.exe from (default: the latest GitHub release)
#   $env:XRESUME_SOURCE     what pip installs for the Python version (default: the GitHub repository)
#   $env:XRESUME_NO_PATH    set to 1 to leave PATH alone

function Install-XResume {
    $releases = if ($env:XRESUME_RELEASES) { $env:XRESUME_RELEASES } else { 'https://github.com/EEkebin/xresume/releases/latest/download' }
    $source = if ($env:XRESUME_SOURCE) { $env:XRESUME_SOURCE } else { 'https://github.com/EEkebin/xresume/archive/refs/heads/main.zip' }
    $root = if ($env:XRESUME_HOME_DIR) { $env:XRESUME_HOME_DIR } else { Join-Path $env:LOCALAPPDATA 'xresume' }
    $venv = Join-Path $root 'venv'
    $bin = Join-Path $root 'bin'
    $exe = Join-Path $bin 'xresume.exe'
    $new = Join-Path $bin 'xresume.new.exe'
    $marker = 'resume a coding-agent conversation'
    New-Item -ItemType Directory -Force -Path $root, $bin | Out-Null

    function Test-XResume($path) {
        # Does this file run here and is it xresume?
        try { $out = & $path --help 2>$null } catch { return $false }
        return ($LASTEXITCODE -eq 0 -and ("$out" -match $marker))
    }

    $installed = $null
    if ($env:XRESUME_NO_BINARY -ne '1' -and [Environment]::Is64BitOperatingSystem) {
        Write-Host 'Downloading xresume...'
        $asset = 'xresume-windows-x64.exe'
        try {
            if ($releases -match '^[A-Za-z][A-Za-z0-9+.-]*://') {
                # A web address (Join-Path cannot be used on one).
                $old = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'
                try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
                try { Invoke-WebRequest -Uri ($releases.TrimEnd('/') + '/' + $asset) -OutFile $new -UseBasicParsing } finally { $ProgressPreference = $old }
            } else {
                Copy-Item -LiteralPath ([IO.Path]::Combine($releases, $asset)) -Destination $new -Force -ErrorAction Stop
            }
            if (Test-XResume $new) { $installed = 'standalone' }
        } catch {}
        if (-not $installed) {
            Remove-Item -LiteralPath $new -Force -ErrorAction SilentlyContinue
            Write-Host 'No standalone executable could be used here; installing the Python version.'
        }
    }

    if (-not $installed) {
        # Find a real Python 3.8+ (the Microsoft Store placeholder named python.exe does not count).
        $python = $null
        foreach ($try in @(@('py', '-3'), @('python'), @('python3'))) {
            $cmd = $try[0]
            $pre = @($try | Select-Object -Skip 1)
            if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { continue }
            try {
                $found = & $cmd @pre -c 'import sys; print(sys.executable if sys.version_info >= (3, 8) else chr(0))' 2>$null
            } catch { $found = $null }
            if ($LASTEXITCODE -eq 0 -and $found -and (Test-Path -LiteralPath "$found")) { $python = "$found"; break }
        }
        if (-not $python) {
            Write-Host 'xresume: Python 3.8 or newer is required for the Python version and was not found.' -ForegroundColor Red
            return
        }
        & $python -m venv --clear $venv
        if ($LASTEXITCODE -ne 0) { Write-Host 'xresume: could not create its Python environment.' -ForegroundColor Red; return }
        & (Join-Path $venv 'Scripts\python.exe') -m pip install --quiet --disable-pip-version-check $source
        if ($LASTEXITCODE -ne 0) { Write-Host "xresume: installing from $source failed." -ForegroundColor Red; return }
        $built = Join-Path $venv 'Scripts\xresume.exe'
        if (-not (Test-Path -LiteralPath $built)) { Write-Host 'xresume: the install did not produce xresume.exe.' -ForegroundColor Red; return }
        # Only xresume.exe goes on PATH, not the environment's own python and pip.
        Copy-Item -LiteralPath $built -Destination $new -Force
        $installed = 'Python'
    }

    try {
        Move-Item -LiteralPath $new -Destination $exe -Force -ErrorAction Stop
    } catch {
        Remove-Item -LiteralPath $new -Force -ErrorAction SilentlyContinue
        Write-Host "xresume: could not replace $exe. Close any running xresume and run this again." -ForegroundColor Red
        return
    }
    Write-Host "Installed $exe ($installed version)"

    if ($env:XRESUME_NO_PATH -eq '1') { return }
    # Read the PATH exactly as stored (entries like %USERPROFILE%\bin stay unexpanded) and only add to it.
    $key = Get-Item -LiteralPath 'HKCU:\Environment'
    $userPath = "$($key.GetValue('Path', '', 'DoNotExpandEnvironmentNames'))"
    $have = @($userPath -split ';' | Where-Object { $_ } | ForEach-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') })
    if ($have -notcontains $bin.TrimEnd('\')) {
        [Environment]::SetEnvironmentVariable('Path', ((@($bin) + @($userPath -split ';' | Where-Object { $_ })) -join ';'), 'User')
        Write-Host "Added $bin to your PATH. Open a new terminal, then run: xresume"
    } else {
        Write-Host 'Run: xresume'
    }
    if (@($env:Path -split ';') -notcontains $bin) { $env:Path = "$bin;$env:Path" }
}

Install-XResume
