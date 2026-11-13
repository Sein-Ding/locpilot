#Requires -Version 5.1
<#
.SYNOPSIS
  LocPilot Windows 一键构建（PyInstaller）。

.DESCRIPTION
  在干净的 Windows 10/11 上从零构建 windows\dist\LocPilot.exe：

    1. 解析 Python 解释器（>= 3.9，推荐 3.11/3.12，64 位）
    2. 幂等创建/复用 windows\.venv 虚拟环境
    3. 幂等安装 windows\requirements.txt + PyInstaller + Pillow
       （依赖都已就绪时不会联网，可直接重复执行）
    4. 调用 windows\locpilot.spec 打包（默认 onefile；-OneDir 可切 onedir）
    5. 校验产物，并默认执行冻结包自检 LocPilot.exe --selftest

  任何一步失败都以非零退出码终止。成功时打印产物路径、大小与 SHA256。

.PARAMETER Python
  指定 Python 解释器（命令名或绝对路径）。缺省依次尝试 py -3 / python / python3。

.PARAMETER VenvDir
  虚拟环境目录，缺省 windows\.venv。

.PARAMETER OutputDir
  产物目录，缺省 windows\dist。

.PARAMETER OneDir
  改为 onedir 产物：windows\dist\LocPilot\LocPilot.exe。启动更快，便于排障；
  对外分发仍建议默认 onefile。

.PARAMETER Clean
  先清理 windows\.build 与旧产物再构建（等价 PyInstaller --clean，不做增量）。

.PARAMETER SkipInstall
  完全不联网：跳过 pip install，要求虚拟环境与依赖已就绪。

.PARAMETER UpgradeDeps
  强制 pip --upgrade（即使依赖已安装）。

.PARAMETER IndexUrl
  pip 镜像地址，例如 https://pypi.tuna.tsinghua.edu.cn/simple 。

.PARAMETER NoSelfTest
  跳过构建后的 LocPilot.exe --selftest 自检（不推荐）。

.PARAMETER Verify
  构建前额外运行 windows\verify.py（源码级端到端冒烟，8 项断言）。

.PARAMETER NoIcon
  跳过图标生成：exe 使用 PyInstaller 默认图标。

.PARAMETER HiddenImports
  额外 hiddenimports（逗号分隔），仅排障时使用。

.EXAMPLE
  .\windows\build.ps1

.EXAMPLE
  .\windows\build.ps1 -Clean -SelfTest

.EXAMPLE
  .\windows\build.ps1 -WhatIf
  只做只读预检并打印将执行的步骤与命令，不修改任何文件。
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = '构建脚本是给人看的 CLI，需要彩色进度输出，不是可管道化的 cmdlet')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', 'Python', Justification = 'Python 参数在 Resolve-PythonHost 函数内按脚本作用域使用，静态分析看不到')]
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Python = "",
    [string]$VenvDir = "",
    [string]$OutputDir = "",
    [switch]$OneDir,
    [switch]$Clean,
    [switch]$SkipInstall,
    [switch]$UpgradeDeps,
    [string]$IndexUrl = "",
    [switch]$NoSelfTest,
    [switch]$Verify,
    [switch]$NoIcon,
    [string[]]$HiddenImports = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# 让 python / pip / PyInstaller 的输出在中文 Windows 上也按 UTF-8 解码
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { $null = $_.Exception }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'

$ScriptDir = $PSScriptRoot
$Root = Split-Path -Parent $ScriptDir
$SpecFile = Join-Path $ScriptDir 'locpilot.spec'
$AppScript = Join-Path $ScriptDir 'app.py'
$VerifyScript = Join-Path $ScriptDir 'verify.py'
$RequirementsFile = Join-Path $ScriptDir 'requirements.txt'
$WorkPath = Join-Path $ScriptDir '.build\pyinstaller'
$IsWindowsHost = ([Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT)

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host ("==> " + $Message) -ForegroundColor Cyan
}

function Write-Info {
    param([string]$Message)
    Write-Host ("    " + $Message)
}

function Write-Ok {
    param([string]$Message)
    Write-Host ("    [OK] " + $Message) -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host ("    [WARN] " + $Message) -ForegroundColor Yellow
}

function Write-Fail {
    param([string]$Message)
    Write-Host ("    [FAIL] " + $Message) -ForegroundColor Red
}

function Invoke-Native {
    param(
        [string]$Exe,
        [string[]]$Arguments,
        [string]$WorkDir = $Root,
        [switch]$AllowFailure
    )
    Write-Host ("    > " + $Exe + " " + ($Arguments -join ' ')) -ForegroundColor DarkGray
    $previous = $ErrorActionPreference
    $code = 1
    $ErrorActionPreference = 'Continue'
    try {
        Push-Location -LiteralPath $WorkDir
        & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host ("    " + $_) }
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $previous
    }
    if (-not $AllowFailure -and $code -ne 0) {
        throw ("命令失败（exit " + $code + "）：" + $Exe + " " + ($Arguments -join ' '))
    }
    return $code
}

function Invoke-Probe {
    param([string]$Exe, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $text = ''
    $code = 1
    $ErrorActionPreference = 'Continue'
    try {
        $text = (& $Exe @Arguments 2>&1 | Out-String)
        $code = $LASTEXITCODE
    } catch {
        $text = $_.Exception.Message
        $code = 1
    } finally {
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{ Code = $code; Text = $text.Trim() }
}

function Resolve-PythonHost {
    $candidates = New-Object System.Collections.ArrayList
    if ($Python) {
        $command = Get-Command $Python -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $command) {
            [void]$candidates.Add([pscustomobject]@{ File = $command.Source; Prefix = @(); Label = $Python })
        } elseif (Test-Path -LiteralPath $Python) {
            $resolved = (Resolve-Path -LiteralPath $Python).Path
            [void]$candidates.Add([pscustomobject]@{ File = $resolved; Prefix = @(); Label = $Python })
        } else {
            throw ("找不到 -Python 指定的解释器：" + $Python)
        }
    }
    foreach ($name in @('py', 'python', 'python3')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $command) { continue }
        if ($name -eq 'py') {
            [void]$candidates.Add([pscustomobject]@{ File = $command.Source; Prefix = @('-3'); Label = 'py -3' })
        } else {
            [void]$candidates.Add([pscustomobject]@{ File = $command.Source; Prefix = @(); Label = $name })
        }
    }
    foreach ($candidate in $candidates) {
        $probeArgs = @()
        $probeArgs += $candidate.Prefix
        $probeArgs += @('-c', "import sys; sys.stdout.write('%d.%d' % sys.version_info[:2])")
        $probe = Invoke-Probe -Exe $candidate.File -Arguments $probeArgs
        if ($probe.Code -ne 0 -or -not $probe.Text) { continue }
        $parts = $probe.Text.Trim().Split('.')
        if ($parts.Length -lt 2) { continue }
        $major = [int]$parts[0]
        $minor = [int]$parts[1]
        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 9)) {
            throw ("Python 版本过低：" + $probe.Text.Trim() + "（" + $candidate.File + "）。需要 Python 3.9 或更高。")
        }
        $bitProbe = Invoke-Probe -Exe $candidate.File -Arguments @($candidate.Prefix + @('-c', "import struct, sys; sys.stdout.write(str(struct.calcsize('P') * 8))"))
        $bits = 0
        if ($bitProbe.Code -eq 0 -and $bitProbe.Text) { $bits = [int]$bitProbe.Text.Trim() }
        if ($bits -ne 64) {
            throw ("需要 64 位 Python，当前解释器为 " + [string]$bits + " 位：" + $candidate.File)
        }
        Write-Ok ("Python: " + $probe.Text.Trim() + " (" + [string]$bits + "-bit) " + $candidate.File)
        return [pscustomobject]@{
            File = $candidate.File
            Prefix = $candidate.Prefix
            Label = $candidate.Label
            Version = $probe.Text.Trim()
            Bits = $bits
        }
    }
    throw "找不到可用的 Python 解释器。请安装 Python 3.9+ 并勾选 Add python.exe to PATH，或用 -Python 指定路径。"
}

function Test-PythonModule {
    param([string]$ModuleName)
    $code = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('" + $ModuleName + "') else 3)"
    $probe = Invoke-Probe -Exe $VenvPython -Arguments @('-c', $code)
    return ($probe.Code -eq 0)
}

function Test-WebView2Runtime {
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
        'HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    )
    foreach ($key in $keys) {
        if (-not (Test-Path -LiteralPath $key)) { continue }
        $item = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
        if ($null -eq $item) { continue }
        $property = $item.PSObject.Properties['pv']
        if ($null -ne $property -and $property.Value) { return [string]$property.Value }
    }
    return ''
}

function Get-ArtifactPath {
    if ($OneDir) {
        return (Join-Path (Join-Path $OutputDir 'LocPilot') 'LocPilot.exe')
    }
    return (Join-Path $OutputDir 'LocPilot.exe')
}

try {
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " LocPilot Windows 构建（PyInstaller）" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Info ("PowerShell : " + $PSVersionTable.PSVersion.ToString() + "  /  OS: " + [Environment]::OSVersion.VersionString)
    Write-Info ("仓库根     : " + $Root)
    Write-Info ("构建脚本   : " + $PSCommandPath)

    if (-not $IsWindowsHost) {
        if (-not $WhatIfPreference) {
            throw ("build.ps1 只能在 Windows 上执行（当前平台：" + [Environment]::OSVersion.VersionString + "）。只做语法/流程检查请加 -WhatIf。")
        }
        Write-Warn "当前不是 Windows：-WhatIf 只做只读预检与计划打印，不会执行任何构建动作。"
    }

    # ------------------------------------------------------------------
    # 输入校验（只读）
    # ------------------------------------------------------------------
    Write-Step "检查源码与构建输入"
    foreach ($required in @($AppScript, $SpecFile, $RequirementsFile, (Join-Path $Root 'locpilot\__init__.py'))) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw ("缺少必需文件：" + $required + "（请在仓库根执行 windows\build.ps1）")
        }
        Write-Ok ("存在 " + $required)
    }

    $Version = '0.0.0'
    $initText = Get-Content -LiteralPath (Join-Path $Root 'locpilot\__init__.py') -Raw
    $versionMatch = [regex]::Match($initText, '__version__\s*=\s*"([^"]+)"')
    if ($versionMatch.Success) {
        $Version = $versionMatch.Groups[1].Value.Trim()
        Write-Ok ("版本号 " + $Version + "（locpilot\__init__.py）")
    } else {
        Write-Warn "未能从 locpilot\__init__.py 解析 __version__，回退 0.0.0"
    }

    $UiDir = Join-Path $ScriptDir 'ui'
    $WebFallback = Join-Path $Root 'web'
    $UiSource = ''
    if (Test-Path -LiteralPath (Join-Path $UiDir 'index.html')) {
        $UiSource = $UiDir
    } elseif (Test-Path -LiteralPath (Join-Path $WebFallback 'index.html')) {
        $UiSource = $WebFallback
        Write-Warn "windows\ui\index.html 不存在，将回退打包仓库 web\（不是 Windows 版 UI，仅供早期联调）"
    } else {
        throw "找不到 UI：windows\ui\index.html 与 web\index.html 都不存在。"
    }
    $UiFileCount = @(Get-ChildItem -LiteralPath $UiSource -Recurse -File -ErrorAction SilentlyContinue).Count
    Write-Ok ("UI 源目录 " + $UiSource + "（" + [string]$UiFileCount + " 个文件 → 打包为 web/）")

    Write-Step "解析 Python 解释器"
    $PythonHost = Resolve-PythonHost

    if (-not $VenvDir) {
        $VenvDir = Join-Path $ScriptDir '.venv'
    } elseif (-not [System.IO.Path]::IsPathRooted($VenvDir)) {
        $VenvDir = Join-Path $Root $VenvDir
    }
    $VenvDir = [System.IO.Path]::GetFullPath($VenvDir)
    $VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
    $VenvReady = Test-Path -LiteralPath $VenvPython
    if ($VenvReady) {
        Write-Ok ("虚拟环境 " + $VenvDir + "（已存在，复用以保持幂等）")
    } else {
        Write-Info ("虚拟环境 " + $VenvDir + "（尚未创建）")
    }

    if (-not $OutputDir) {
        $OutputDir = Join-Path $ScriptDir 'dist'
    } elseif (-not [System.IO.Path]::IsPathRooted($OutputDir)) {
        $OutputDir = Join-Path $Root $OutputDir
    }
    $OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
    $ArtifactPath = Get-ArtifactPath
    $WorkPath = [System.IO.Path]::GetFullPath($WorkPath)

    $RequiredModules = @(
        [pscustomobject]@{ Module = 'webview'; Label = 'pywebview' },
        [pscustomobject]@{ Module = 'PyInstaller'; Label = 'PyInstaller' },
        [pscustomobject]@{ Module = 'PIL'; Label = 'Pillow（用于生成图标）' },
        [pscustomobject]@{ Module = 'pymobiledevice3'; Label = 'pymobiledevice3（真机引擎）' }
    )
    $Missing = @()
    if ($VenvReady) {
        foreach ($module in $RequiredModules) {
            if (-not (Test-PythonModule -ModuleName $module.Module)) { $Missing += $module }
        }
        if ($Missing.Count -eq 0) {
            Write-Ok "依赖已就绪（重复执行不会联网）"
        } else {
            Write-Info ("缺少依赖：" + (($Missing | ForEach-Object { $_.Label }) -join ', '))
        }
    } else {
        $Missing = $RequiredModules
    }

    if ($IsWindowsHost) {
        $webview2Version = Test-WebView2Runtime
        if ($webview2Version) {
            Write-Ok ("WebView2 Runtime " + $webview2Version)
        } else {
            Write-Warn "未检测到 WebView2 Runtime（Win11 预装；Win10 请安装：https://developer.microsoft.com/microsoft-edge/webview2/）。不影响构建，但 exe 在目标机可能白屏。"
        }
    }

    # ------------------------------------------------------------------
    # -WhatIf：只打印计划，不做任何写操作
    # ------------------------------------------------------------------
    if ($WhatIfPreference) {
        Write-Step "WhatIf 计划（不会执行任何写操作）"
        Write-Info ("1. 虚拟环境：" + $VenvDir + "（" + ($(if ($VenvReady) { '已存在，复用' } else { '将创建' })) + "）")
        if ($SkipInstall) {
            Write-Info "2. 依赖：-SkipInstall，跳过 pip install（要求已就绪）"
        } elseif ($Missing.Count -eq 0 -and -not $UpgradeDeps) {
            Write-Info "2. 依赖：已齐备，跳过 pip install"
        } else {
            Write-Info ("2. 依赖：pip install（补齐：" + (($Missing | ForEach-Object { $_.Label }) -join ', ') + "）")
        }
        Write-Info ("3. 清理：" + ($(if ($Clean) { '删除 ' + $WorkPath + ' 与旧产物' } else { '不做（增量）' })))
        if ($Verify) {
            Write-Info ("4. 源码级冒烟：python " + $VerifyScript)
        } else {
            Write-Info "4. 源码级冒烟：跳过（加 -Verify 可运行 windows\verify.py）"
        }
        Write-Info ("5. 打包：" + $VenvPython + " -m PyInstaller --noconfirm --distpath " + $OutputDir + " --workpath " + $WorkPath + " " + $SpecFile)
        Write-Info ("   模式：" + ($(if ($OneDir) { 'onedir' } else { 'onefile' })) + " / 版本：" + $Version + " / UI：" + $UiSource)
        Write-Info ("   图标：" + ($(if ($NoIcon) { '跳过（-NoIcon）' } else { '自动生成或复用 windows\assets\locpilot.ico' })))
        Write-Info ("6. 产物：" + $ArtifactPath)
        Write-Info ("7. 冻结包自检：" + ($(if ($NoSelfTest) { '跳过（-NoSelfTest）' } else { $ArtifactPath + ' --selftest（exit 0，且日志出现 backend=ok / ui=ok / frozen=True）' })))
        Write-Host ""
        Write-Ok "WhatIf 检查完成：脚本可解析、预检通过、未修改任何文件。"
        exit 0
    }

    # ------------------------------------------------------------------
    # 1) 虚拟环境（幂等）
    # ------------------------------------------------------------------
    if (-not $VenvReady) {
        Write-Step ("创建虚拟环境 " + $VenvDir)
        if (Test-Path -LiteralPath $VenvDir) {
            Write-Warn ("目录存在但缺少 " + $VenvPython + "，删除残缺 venv 后重建")
            Remove-Item -LiteralPath $VenvDir -Recurse -Force
        }
        $venvArgs = @()
        $venvArgs += $PythonHost.Prefix
        $venvArgs += @('-m', 'venv', $VenvDir)
        Invoke-Native -Exe $PythonHost.File -Arguments $venvArgs -WorkDir $Root | Out-Null
        if (-not (Test-Path -LiteralPath $VenvPython)) {
            throw ("虚拟环境创建失败：" + $VenvPython + " 不存在")
        }
        $VenvReady = $true
        $Missing = $RequiredModules
        Write-Ok ("虚拟环境就绪：" + $VenvPython)
    }

    # ------------------------------------------------------------------
    # 2) 依赖（幂等：齐备则完全跳过联网）
    # ------------------------------------------------------------------
    if (-not $SkipInstall) {
        $needInstall = ($Missing.Count -gt 0) -or $UpgradeDeps
        if (-not $needInstall) {
            Write-Step "依赖检查"
            Write-Ok "依赖已齐备，跳过 pip install（幂等）"
        } else {
            Write-Step "安装依赖（PyPI）"
            $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input')
            if ($IndexUrl) { $pipArgs += @('--index-url', $IndexUrl) }
            if ($UpgradeDeps) { $pipArgs += '--upgrade' }
            $pipArgs += @('-r', $RequirementsFile, 'pyinstaller>=6.3', 'pillow>=9.0')
            Invoke-Native -Exe $VenvPython -Arguments $pipArgs -WorkDir $Root | Out-Null
            $stillMissing = @()
            foreach ($module in $RequiredModules) {
                if (-not (Test-PythonModule -ModuleName $module.Module)) { $stillMissing += $module.Label }
            }
            if ($stillMissing.Count -gt 0) {
                throw ("pip 安装后仍缺少依赖：" + ($stillMissing -join ', ') + "。请检查网络/镜像（-IndexUrl）或 Python 版本。")
            }
            Write-Ok "依赖安装完成"
        }
    } else {
        Write-Step "依赖检查（-SkipInstall）"
        if (-not $VenvReady -or $Missing.Count -gt 0) {
            throw "-SkipInstall 要求虚拟环境与依赖已就绪；请先不带 -SkipInstall 执行一次。"
        }
        Write-Ok "依赖已就绪，按 -SkipInstall 跳过 pip install"
    }

    $EnvProbe = Invoke-Probe -Exe $VenvPython -Arguments @(
        '-c',
        "import sys, PyInstaller, PIL, webview; import pymobiledevice3 as _pmd3; print('python=' + sys.version.split()[0]); print('pyinstaller=' + PyInstaller.__version__); print('pywebview=' + getattr(webview, '__version__', '?')); print('pillow=' + PIL.__version__); print('pymobiledevice3=' + getattr(_pmd3, '__version__', '?'))"
    )
    if ($EnvProbe.Code -eq 0) {
        foreach ($line in ($EnvProbe.Text -split [Environment]::NewLine)) {
            if ($line.Trim()) { Write-Ok ("依赖版本 " + $line.Trim()) }
        }
    } else {
        Write-Warn ("无法读取依赖版本：" + $EnvProbe.Text)
    }

    # ------------------------------------------------------------------
    # 3) 可选清理
    # ------------------------------------------------------------------
    if ($Clean) {
        Write-Step "清理旧构建产物（-Clean）"
        if (Test-Path -LiteralPath $WorkPath) {
            Remove-Item -LiteralPath $WorkPath -Recurse -Force
            Write-Ok ("已删除 " + $WorkPath)
        }
        if (Test-Path -LiteralPath $ArtifactPath) {
            Remove-Item -LiteralPath $ArtifactPath -Recurse -Force
            Write-Ok ("已删除旧产物 " + $ArtifactPath)
        }
    } else {
        Write-Step "增量构建（未指定 -Clean）"
        Write-Info ("工作目录 " + $WorkPath + "；如需彻底重建请加 -Clean")
    }

    # ------------------------------------------------------------------
    # 4) 可选源码级冒烟（windows\verify.py）
    # ------------------------------------------------------------------
    if ($Verify) {
        Write-Step "源码级端到端冒烟：windows\verify.py"
        $verifyCode = Invoke-Native -Exe $VenvPython -Arguments @($VerifyScript) -WorkDir $Root -AllowFailure
        if ($verifyCode -ne 0) {
            throw ("windows\verify.py 失败（exit " + [string]$verifyCode + "）")
        }
        Write-Ok "verify.py 全部通过（mock 引擎：health/status/connect/teleport/UI/clear）"
    }

    # ------------------------------------------------------------------
    # 5) PyInstaller 打包
    # ------------------------------------------------------------------
    Write-Step ("PyInstaller 打包（" + ($(if ($OneDir) { 'onedir' } else { 'onefile' })) + "）")
    $env:LOCPILOT_BUILD_VERSION = $Version
    if ($OneDir) {
        $env:LOCPILOT_WIN_ONEDIR = '1'
    } else {
        Remove-Item -LiteralPath 'Env:LOCPILOT_WIN_ONEDIR' -ErrorAction SilentlyContinue
    }
    if ($NoIcon) {
        $env:LOCPILOT_WIN_NO_ICON = '1'
    } else {
        Remove-Item -LiteralPath 'Env:LOCPILOT_WIN_NO_ICON' -ErrorAction SilentlyContinue
    }
    if ($HiddenImports.Count -gt 0) {
        $env:LOCPILOT_HIDDEN_IMPORTS = ($HiddenImports -join ',')
    } else {
        Remove-Item -LiteralPath 'Env:LOCPILOT_HIDDEN_IMPORTS' -ErrorAction SilentlyContinue
    }

    $pyinstallerArgs = @('-m', 'PyInstaller', '--noconfirm', '--log-level', 'INFO')
    if ($Clean) { $pyinstallerArgs += '--clean' }
    $pyinstallerArgs += @('--distpath', $OutputDir, '--workpath', $WorkPath, $SpecFile)
    Invoke-Native -Exe $VenvPython -Arguments $pyinstallerArgs -WorkDir $Root | Out-Null

    # ------------------------------------------------------------------
    # 6) 产物校验
    # ------------------------------------------------------------------
    Write-Step "校验构建产物"
    if (-not (Test-Path -LiteralPath $ArtifactPath)) {
        throw ("构建结束但没有找到产物：" + $ArtifactPath + "（检查 PyInstaller 输出中的 ERROR）")
    }
    $artifactItem = Get-Item -LiteralPath $ArtifactPath
    if ($artifactItem.Length -lt 1MB) {
        throw ("产物异常偏小（" + [string]$artifactItem.Length + " 字节）：" + $ArtifactPath)
    }
    $artifactHash = (Get-FileHash -LiteralPath $ArtifactPath -Algorithm SHA256).Hash
    Write-Ok ("产物 " + $ArtifactPath + "（" + [string][math]::Round($artifactItem.Length / 1MB, 1) + " MB）")
    Write-Ok ("SHA256 " + $artifactHash)

    # ------------------------------------------------------------------
    # 7) 冻结包自检（不开窗口）
    # ------------------------------------------------------------------
    if (-not $NoSelfTest) {
        Write-Step "冻结包自检：LocPilot.exe --selftest（不开窗口）"
        $logPath = ''
        if ($env:LOCALAPPDATA) {
            $logPath = Join-Path $env:LOCALAPPDATA 'LocPilot\locpilot.log'
        }
        $linesBefore = 0
        if ($logPath -and (Test-Path -LiteralPath $logPath)) {
            $linesBefore = @(Get-Content -LiteralPath $logPath).Count
        }
        $process = Start-Process -FilePath $ArtifactPath -ArgumentList '--selftest' -PassThru
        if (-not $process.WaitForExit(180000)) {
            try { $process.Kill() } catch { Write-Warn ("无法结束自检进程：" + $_.Exception.Message) }
            throw "LocPilot.exe --selftest 超过 180 秒未退出"
        }
        $selftestExit = $process.ExitCode
        $newLines = @()
        if ($logPath -and (Test-Path -LiteralPath $logPath)) {
            $allLines = @(Get-Content -LiteralPath $logPath -Encoding UTF8)
            if ($allLines.Count -gt $linesBefore) {
                $newLines = @($allLines | Select-Object -Skip $linesBefore)
            }
        }
        foreach ($line in $newLines) {
            if ([string]$line -ne '') { Write-Info ("[log] " + $line) }
        }
        if ($selftestExit -ne 0) {
            throw ("冻结包自检失败（--selftest exit " + [string]$selftestExit + "）。日志：" + $logPath)
        }
        if ($newLines.Count -gt 0) {
            $backendLine = @($newLines | Where-Object { [string]$_ -like 'backend=*' }) | Select-Object -Last 1
            $uiLine = @($newLines | Where-Object { [string]$_ -like 'ui=*' }) | Select-Object -Last 1
            $frozenLine = @($newLines | Where-Object { [string]$_ -like 'frozen=*' }) | Select-Object -Last 1
            if ([string]$backendLine -notlike '*=ok') {
                throw ("冻结包自检 backend 异常：" + [string]$backendLine)
            }
            if ([string]$uiLine -notlike '*=ok') {
                throw ("冻结包自检 ui 异常：" + [string]$uiLine + "（检查 windows/ui 是否完整打包，或 exe 是否被杀软拦截）")
            }
            if ([string]$frozenLine -notlike '*=True*') {
                throw ("冻结包自检未处于 frozen 模式：" + [string]$frozenLine)
            }
            Write-Ok ("自检通过：" + [string]$backendLine + " / " + [string]$uiLine + " / " + [string]$frozenLine)
        } else {
            Write-Warn ("自检 exit=0，但未读到新的自检日志（" + $logPath + "）；请手动运行 exe --selftest 复核")
        }
    } else {
        Write-Step "跳过冻结包自检（-NoSelfTest）"
    }

    # ------------------------------------------------------------------
    # 完成
    # ------------------------------------------------------------------
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host " 构建成功" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Info ("产物     : " + $ArtifactPath)
    Write-Info ("大小     : " + [string][math]::Round($artifactItem.Length / 1MB, 1) + " MB")
    Write-Info ("SHA256   : " + $artifactHash)
    Write-Info ("版本     : " + $Version + "（" + ($(if ($OneDir) { 'onedir' } else { 'onefile' })) + "）")
    Write-Info ("Python   : " + $PythonHost.Version + "（" + $VenvPython + "）")
    Write-Info ("UI 源    : " + $UiSource)
    Write-Host ""
    Write-Info ("运行     : " + $ArtifactPath)
    Write-Info ("自检     : " + $ArtifactPath + " --selftest")
    Write-Info "日志     : %LOCALAPPDATA%\LocPilot\locpilot.log"
    Write-Info "说明     : windows\README.md"
    exit 0
} catch {
    Write-Host ""
    Write-Fail ($_.Exception.Message)
    if ($_.ScriptStackTrace) {
        Write-Host ("    " + $_.ScriptStackTrace) -ForegroundColor DarkGray
    }
    Write-Host "    构建失败。常见原因与处理见 windows\README.md 的「故障排查」。" -ForegroundColor Red
    exit 1
}
