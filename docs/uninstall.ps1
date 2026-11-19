# xresume uninstaller for Windows.
#
#   irm https://eekebin.github.io/xresume/uninstall.ps1 | iex
#
# Removes what install.ps1 put on this PC: %LOCALAPPDATA%\xresume and its bin folder on your user
# PATH. It also removes ~/.xresume, where xresume keeps its links between copies and its backups.
# Sessions xresume created stay in each CLI's own history.
#
#   $env:XRESUME_HOME_DIR   the folder xresume was installed into (default %LOCALAPPDATA%\xresume)
#   $env:XRESUME_HOME       xresume's data folder (default ~/.xresume)

function Uninstall-XResume {
    $root = if ($env:XRESUME_HOME_DIR) { $env:XRESUME_HOME_DIR } else { Join-Path $env:LOCALAPPDATA 'xresume' }
    $bin = Join-Path $root 'bin'
    $data = if ($env:XRESUME_HOME) { $env:XRESUME_HOME } else { Join-Path $HOME '.xresume' }
    $removed = @()

    if (Test-Path -LiteralPath $root) {
        # Only a folder that holds an xresume install (in case XRESUME_HOME_DIR points somewhere else).
        if (-not ((Test-Path -LiteralPath (Join-Path $bin 'xresume.exe')) -or (Test-Path -LiteralPath (Join-Path $root 'venv\Scripts\xresume.exe')))) {
            Write-Host "xresume: $root does not look like an xresume install. Nothing was changed." -ForegroundColor Red
            return
        }
        try {
            Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction Stop
            $removed += $root
        } catch {
            Write-Host "xresume: could not remove $root. Close any running xresume and run this again." -ForegroundColor Red
            return
        }
    }

    # Take only xresume's folder out of the PATH, reading and writing it exactly as stored.
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    $userPath = "$($key.GetValue('Path', '', 'DoNotExpandEnvironmentNames'))"
    $all = @($userPath -split ';' | Where-Object { $_ })
    $keep = @($all | Where-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') -ne $bin.TrimEnd('\') })
    if ($keep.Count -ne $all.Count) {
        if ($keep.Count) { $key.SetValue('Path', ($keep -join ';'), 'ExpandString') } else { $key.DeleteValue('Path', $false) }
        # Setting and clearing a variable this way tells open programs the environment changed.
        [Environment]::SetEnvironmentVariable('XRESUME_PATH_CHANGED', '1', 'User')
        [Environment]::SetEnvironmentVariable('XRESUME_PATH_CHANGED', $null, 'User')
        $removed += "$bin from your PATH"
    }
    $env:Path = (@($env:Path -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ne $bin.TrimEnd('\') }) -join ';')

    if (Test-Path -LiteralPath $data) {
        Remove-Item -LiteralPath $data -Recurse -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path -LiteralPath $data)) { $removed += $data }
    }

    if ($removed.Count) {
        $removed | ForEach-Object { Write-Host "Removed $_" }
        Write-Host 'xresume is uninstalled.'
    } else {
        Write-Host 'xresume is not installed here; nothing to remove.'
    }
}

Uninstall-XResume
