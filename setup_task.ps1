# ============================================================================
# 配置 Windows 定时任务 — 网易云音乐合伙人自动评分
# ============================================================================
# 用法（管理员 PowerShell）:
#   .\setup_task.ps1
#   .\setup_task.ps1 -Hour 9 -Minute 30
#   .\setup_task.ps1 -Uninstall
# ============================================================================

param(
    [ValidateRange(0, 23)]
    [int]$Hour = 9,
    [ValidateRange(0, 59)]
    [int]$Minute = 30,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "NeteaseMusicPartner"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptPath = Join-Path $ScriptDir "music_partner.py"
$ConfigPath = Join-Path $ScriptDir "copartner_ck.json"
$RequirementsPath = Join-Path $ScriptDir "requirements.txt"
$WorkingDir = $ScriptDir

if ($Uninstall) {
    try {
        $ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $ExistingTask) {
            Write-Host "定时任务不存在: $TaskName"
            exit 0
        }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            throw "删除后仍能查询到计划任务"
        }
        Write-Host "定时任务已删除"
        exit 0
    }
    catch {
        Write-Error ("删除定时任务失败: " + $_.Exception.Message)
        exit 1
    }
}

try {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        $PythonCommand = Get-Command python3 -ErrorAction SilentlyContinue
    }
    if (-not $PythonCommand) {
        throw "未找到 Python，请先安装 Python 并添加到 PATH"
    }
    $PythonExe = $PythonCommand.Source

    if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) {
        throw "找不到 music_partner.py"
    }
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "配置文件 copartner_ck.json 不存在，请先创建并填入 Cookie"
    }
    if (-not (Test-Path -LiteralPath $RequirementsPath -PathType Leaf)) {
        throw "找不到 requirements.txt"
    }

    Write-Host "检查 Python 依赖..."
    & $PythonExe -c "import Crypto, requests" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "依赖未满足，开始安装 requirements.txt..."
        & $PythonExe -m pip install -r $RequirementsPath
        if ($LASTEXITCODE -ne 0) {
            throw "pip 安装失败，退出码 $LASTEXITCODE"
        }
        & $PythonExe -c "import Crypto, requests" 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "依赖安装后 import 复检失败"
        }
    }

    $ExpectedArguments = "`"$ScriptPath`" --config `"$ConfigPath`""
    $Action = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument $ExpectedArguments `
        -WorkingDirectory $WorkingDir `
        -ErrorAction Stop

    $Trigger = New-ScheduledTaskTrigger -Daily -At ("{0:D2}:{1:D2}" -f $Hour, $Minute) -ErrorAction Stop
    $CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $Principal = New-ScheduledTaskPrincipal `
        -UserId $CurrentUser `
        -LogonType Interactive `
        -RunLevel Limited `
        -ErrorAction Stop

    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ErrorAction Stop

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Principal $Principal `
        -Settings $Settings `
        -Description "网易云音乐合伙人每日自动评分" `
        -Force `
        -ErrorAction Stop | Out-Null

    $RegisteredTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $RegisteredAction = @($RegisteredTask.Actions)[0]
    if (-not $RegisteredAction) {
        throw "注册后未找到任务动作"
    }
    if ([IO.Path]::GetFullPath($RegisteredAction.Execute) -ne [IO.Path]::GetFullPath($PythonExe)) {
        throw "注册后的执行文件不匹配"
    }
    if ($RegisteredAction.Arguments -ne $ExpectedArguments) {
        throw "注册后的参数不匹配"
    }
    if ([IO.Path]::GetFullPath($RegisteredAction.WorkingDirectory) -ne [IO.Path]::GetFullPath($WorkingDir)) {
        throw "注册后的工作目录不匹配"
    }

    Write-Host ""
    Write-Host "定时任务已创建并验证!" -ForegroundColor Green
    Write-Host "  任务名称: $TaskName"
    Write-Host "  执行时间: 每天 ${Hour}:$(($Minute).ToString('00'))"
    Write-Host "  脚本: music_partner.py"
    exit 0
}
catch {
    Write-Error ("创建定时任务失败: " + $_.Exception.Message)
    exit 1
}
