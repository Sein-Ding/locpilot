#Requires -Version 5.1
<#
.SYNOPSIS
  LocPilot Windows 开发模式：不打包，直接用 pywebview 窗口运行 windows\app.py。

.DESCRIPTION
  与 windows\build.ps1 共用同一个虚拟环境（默认 windows\.venv）：
  环境不存在就创建，依赖缺失就安装，齐备则直接启动（幂等，可反复执行）。
  修改 python / ui 代码后重新执行本脚本即可生效，不需要重新打包。

  开发模式使用进程内 pymobiledevice3 引擎（app.py 会设置 LOCPILOT_PMD3_INPROC=1），
  没有真机也可以加 -Mock 用虚拟设备把 UI / API / 回放链路跑通。

.PARAMETER Python
  指定 Python 解释器（命令名或绝对路径）。缺省依次尝试 py -3 / python / python3。

.PARAMETER VenvDir
  虚拟环境目录，缺省 windows\.venv（与 build.ps1 相同）。

.PARAMETER Mock
  使用 mock 引擎（虚拟 iPhone，不接触真机），等价设置 LOCPILOT_ENGINE=mock。

.PARAMETER SkipInstall
  不联网：跳过 pip install，要求依赖已就绪。

.PARAMETER UpgradeDeps
  强制 pip --upgrade 依赖。

.PARAMETER IndexUrl
  pip 镜像地址，例如 https://pypi.tuna.tsinghua.edu.cn/simple 。

.PARAMETER SelfTest
  只做无窗口自检：等价 python windows\app.py --selftest（期望 backend=ok / ui=ok）。

.PARAMETER Port
  固定后端端口（调试用；默认 0 = 系统分配）。

.PARAMETER AppArgs
  透传给 windows\app.py 的其它参数，放在命令最后。

.EXAMPLE
  .\windows\run-dev.ps1 -Mock

.EXAMPLE
  .\windows\run-dev.ps1 -SelfTest

.EXAMPLE
  .\windows\run-dev.ps1 -WhatIf
  只打印将执行的步骤，不修改文件、不启动程序。
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = '开发脚本是给人看的 CLI，需要彩色进度输出，不是可管道化的 cmdlet')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', 'Python', Justification = 'Python 参数在 Resolve-PythonHost 函数内按脚本作用域使用，静态分析看不到')]
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Python = "",
    [string]$VenvDir = "",
    [switch]$Mock,
    [switch]$SkipInstall,
    [switch]$UpgradeDeps,
    [string]$IndexUrl = "",
    [switch]$SelfTest,
    [int]$Port = 0,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$AppArgs = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { $null = $_.Exception }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'

$ScriptDir = $PSScriptRoot
$Root = Split-Path -Parent $ScriptDir
$AppScript = Join-Path $ScriptDir 'app.py'
$RequirementsFile = Join-Path $ScriptDir 'requirements.txt'
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
        return [pscustomobject]@{ File = $candidate.File; Prefix = $candidate.Prefix; Version = $probe.Text.Trim() }
    }
    throw "找不到可用的 Python 解释器。请安装 Python 3.9+ 并勾选 Add python.exe to PATH，或用 -Python 指定路径。"
}

function Test-PythonModule {
    param([string]$ModuleName)
    $code = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('" + $ModuleName + "') else 3)"
    $probe = Invoke-Probe -Exe $VenvPython -Arguments @('-c', $code)
    return ($probe.Code -eq 0)
}

try {
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " LocPilot Windows 开发模式（pywebview，不打包）" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Info ("PowerShell : " + $PSVersionTable.PSVersion.ToString())
    Write-Info ("仓库根     : " + $Root)

    if (-not $IsWindowsHost) {
        if (-not $WhatIfPreference) {
            throw ("run-dev.ps1 只能在 Windows 上执行（当前平台：" + [Environment]::OSVersion.VersionString + "）。只做语法/流程检查请加 -WhatIf。")
        }
        Write-Warn "当前不是 Windows：-WhatIf 只做只读预检与计划打印，不会启动程序。"
    }

    Write-Step "检查源码与依赖清单"
    foreach ($required in @($AppScript, $RequirementsFile, (Join-Path $Root 'locpilot\__init__.py'))) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw ("缺少必需文件：" + $required + "（请在仓库根执行 windows\run-dev.ps1）")
        }
        Write-Ok ("存在 " + $required)
    }

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
        Write-Ok ("虚拟环境 " + $VenvDir + "（已存在）")
    } else {
        Write-Info ("虚拟环境 " + $VenvDir + "（尚未创建）")
    }

    $RequiredModules = @(
        [pscustomobject]@{ Module = 'webview'; Label = 'pywebview（窗口外壳）' },
        [pscustomobject]@{ Module = 'pymobiledevice3'; Label = 'pymobiledevice3（真机引擎）' }
    )
    $Missing = @()
    if ($VenvReady) {
        foreach ($module in $RequiredModules) {
            if (-not (Test-PythonModule -ModuleName $module.Module)) { $Missing += $module }
        }
        if ($Missing.Count -eq 0) { Write-Ok "运行期依赖已就绪" }
    } else {
        $Missing = $RequiredModules
    }

    $RunArgs = @()
    if ($SelfTest) { $RunArgs += '--selftest' }
    if ($Port -gt 0) { $RunArgs += @('--port', [string]$Port) }
    if ($AppArgs.Count -gt 0) { $RunArgs += $AppArgs }
    $EngineMode = 'auto（settings.json / 环境变量）'
    if ($Mock) { $EngineMode = 'mock（虚拟设备，-Mock）' }
    if ($SelfTest) { $EngineMode = '不连接（--selftest 只起后端）' }

    if ($WhatIfPreference) {
        Write-Step "WhatIf 计划（不会创建虚拟环境、不会安装依赖、不会启动程序）"
        Write-Info ("1. 虚拟环境：" + $VenvDir + "（" + ($(if ($VenvReady) { '已存在，复用' } else { '将创建' })) + "）")
        if ($SkipInstall) {
            Write-Info "2. 依赖：-SkipInstall，跳过 pip install（要求已就绪）"
        } elseif ($Missing.Count -eq 0 -and -not $UpgradeDeps) {
            Write-Info "2. 依赖：已齐备，跳过 pip install"
        } else {
            Write-Info ("2. 依赖：pip install -r " + $RequirementsFile + "（补齐：" + (($Missing | ForEach-Object { $_.Label }) -join ', ') + "）")
        }
        Write-Info ("3. 引擎：" + $EngineMode)
        Write-Info ("4. 启动：" + $VenvPython + " " + $AppScript + ($(if ($RunArgs.Count -gt 0) { " " + ($RunArgs -join " ") } else { "" })))
        if ($SelfTest) {
            Write-Info "5. 预期：backend=ok / ui=ok，退出码 0（--selftest 不开窗口）"
        } else {
            Write-Info "5. 预期：弹出 LocPilot 原生窗口；关闭窗口后脚本退出"
        }
        Write-Host ""
        Write-Ok "WhatIf 检查完成：脚本可解析、预检通过、未修改任何文件。"
        exit 0
    }

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

    if (-not $SkipInstall) {
        $needInstall = ($Missing.Count -gt 0) -or $UpgradeDeps -or (-not $VenvReady)
        if ($needInstall) {
            Write-Step "安装运行期依赖（PyPI）"
            $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input')
            if ($IndexUrl) { $pipArgs += @('--index-url', $IndexUrl) }
            if ($UpgradeDeps) { $pipArgs += '--upgrade' }
            $pipArgs += @('-r', $RequirementsFile)
            Invoke-Native -Exe $VenvPython -Arguments $pipArgs -WorkDir $Root | Out-Null
            $stillMissing = @()
            foreach ($module in $RequiredModules) {
                if (-not (Test-PythonModule -ModuleName $module.Module)) { $stillMissing += $module.Label }
            }
            if ($stillMissing.Count -gt 0) {
                throw ("pip 安装后仍缺少依赖：" + ($stillMissing -join ', ') + "。请检查网络/镜像（-IndexUrl）。")
            }
            Write-Ok "依赖安装完成"
        } else {
            Write-Step "依赖检查"
            Write-Ok "依赖已齐备，跳过 pip install（幂等）"
        }
    } else {
        Write-Step "依赖检查（-SkipInstall）"
        if ($Missing.Count -gt 0) {
            throw "-SkipInstall 要求依赖已就绪；请先不带 -SkipInstall 执行一次。"
        }
        Write-Ok "依赖已就绪，按 -SkipInstall 跳过 pip install"
    }

    if ($Mock) {
        $env:LOCPILOT_ENGINE = 'mock'
        Write-Ok "引擎已切换为 mock（环境变量 LOCPILOT_ENGINE=mock）"
    } else {
        Write-Info ("引擎：" + $EngineMode)
    }
    Write-Info "开发模式后端运行在 python 进程内；状态目录 %LOCALAPPDATA%\LocPilot"
    Write-Info "修改 windows\ui 或 locpilot 代码后，重新执行本脚本即可生效"

    Write-Step "启动 LocPilot（pywebview 窗口）"
    $exitCode = Invoke-Native -Exe $VenvPython -Arguments (@($AppScript) + $RunArgs) -WorkDir $Root -AllowFailure
    if ($SelfTest -and $exitCode -eq 0) {
        Write-Ok "自检通过（--selftest exit 0）"
    } elseif ($SelfTest) {
        throw ("自检失败（--selftest exit " + [string]$exitCode + "）")
    }
    Write-Host ""
    Write-Info ("LocPilot 退出码：" + [string]$exitCode)
    exit $exitCode
} catch {
    Write-Host ""
    Write-Fail ($_.Exception.Message)
    if ($_.ScriptStackTrace) {
        Write-Host ("    " + $_.ScriptStackTrace) -ForegroundColor DarkGray
    }
    exit 1
}
