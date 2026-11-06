# ensp-mcp 一键部署脚本
# 双击同目录下的“一键部署.bat”运行即可；也可手动执行：
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy.ps1
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$root = $PSScriptRoot

Write-Host "=== ensp-mcp 一键部署 ===" -ForegroundColor Cyan
Write-Host "目录: $root"
Write-Host ""

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Find-VBoxManage {
    if ($env:VBOX_MSI_INSTALL_PATH) {
        $p = Join-Path $env:VBOX_MSI_INSTALL_PATH 'VBoxManage.exe'
        if (Test-Path $p) { return $p }
    }
    $p = 'C:\Program Files\Oracle\VirtualBox\VBoxManage.exe'
    if (Test-Path $p) { return $p }
    $cmd = Get-Command VBoxManage -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# 确保存在处于 Up 状态的 VirtualBox Host-Only 网卡（AR/WLAN 等虚拟设备启动的前提）
function Ensure-HostOnly {
    $vbox = Find-VBoxManage
    if (-not $vbox) {
        Write-Host "未找到 VirtualBox，跳过 Host-Only 网卡检查。" -ForegroundColor Yellow
        return
    }

    $list = & $vbox list hostonlyifs 2>&1 | Out-String
    if ($list -match 'Status:\s+Up') {
        Write-Host "Host-Only 网卡已就绪。" -ForegroundColor Green
        return
    }

    Write-Host "未检测到 Up 状态的 Host-Only 网卡，正在创建 ..." -ForegroundColor Yellow
    try {
        if (Test-Admin) {
            & $vbox hostonlyif create | Out-Null
        } else {
            Write-Host "创建网卡需要管理员权限，正在请求提权（仅此一步）..." -ForegroundColor Yellow
            Start-Process -FilePath $vbox -ArgumentList 'hostonlyif', 'create' -Verb RunAs -Wait
        }
    } catch {
        Write-Host "创建 Host-Only 网卡失败：$($_.Exception.Message)" -ForegroundColor Red
    }

    for ($i = 0; $i -lt 5; $i++) {
        $list = & $vbox list hostonlyifs 2>&1 | Out-String
        if ($list -match 'Status:\s+Up') {
            Write-Host "Host-Only 网卡创建成功。" -ForegroundColor Green
            return
        }
        Start-Sleep -Seconds 1
    }

    Write-Host "仍未检测到 Host-Only 网卡，AR/WLAN 设备将无法启动。请以管理员身份执行：" -ForegroundColor Red
    Write-Host "  & `"$vbox`" hostonlyif create"
}

function Find-Python {
    $candidates = @()
    foreach ($v in @('3.12', '3.11', '3.10', '3.13')) {
        try {
            $p = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $p) { $candidates += $p.Trim() }
        } catch {}
    }
    $local = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (Test-Path $local) { $candidates += $local }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }

    foreach ($c in $candidates) {
        try {
            $ver = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver) {
                $parts = $ver.Trim().Split('.')
                if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10) { return $c }
            }
        } catch {}
    }
    return $null
}

# 1. Python 3.10+
$python = Find-Python
if (-not $python) {
    Write-Host "未找到 Python 3.10+，正在安装 Python 3.12.10 ..." -ForegroundColor Yellow
    $installer = Join-Path $root 'python-3.12.10-amd64.exe'
    if (-not (Test-Path $installer)) { throw "缺少 Python 安装包: $installer" }
    Start-Process -FilePath $installer -ArgumentList '/quiet InstallAllUsers=0 PrependPath=0 InstallLauncherAllUsers=0 Include_test=0 Include_doc=0 Include_tcltk=0' -Wait
    $python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (-not (Test-Path $python)) { throw "Python 安装失败，请手动运行 $installer" }
}
Write-Host "使用 Python: $python" -ForegroundColor Green

# 2. 独立虚拟环境
$venv = Join-Path $env:USERPROFILE '.venvs\ensp-mcp'
$venvPy = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host "创建虚拟环境: $venv" -ForegroundColor Yellow
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败" }
} else {
    Write-Host "虚拟环境已存在: $venv"
}

# 3. 安装 ensp-mcp（优先离线 wheel）
# 先结束可能占用旧版文件的进程，否则 pip 覆盖 ensp-mcp.exe 会报 WinError 32
$busy = @()
$busy += Get-Process ensp-mcp -ErrorAction SilentlyContinue
try {
    $busy += Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -like '*ensp_mcp.server*' } |
        ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
} catch {}
if ($busy) {
    Write-Host "检测到正在运行的 ensp-mcp 进程，先结束它们以便覆盖安装 ..." -ForegroundColor Yellow
    $busy | Where-Object { $_ } | Select-Object -Unique -Property Id | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}
# 清理上次安装中断留下的无效目录（pip 会报 ~nsp-mcp invalid distribution）
Get-ChildItem (Join-Path $venv 'Lib\site-packages') -Filter '~*nsp*' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "安装 ensp-mcp 1.3.0（含命令留痕、批量并发、自动验收）..." -ForegroundColor Yellow
& $venvPy -m pip install --no-index --find-links (Join-Path $root 'wheelhouse') 'ensp-mcp==1.3.0'
if ($LASTEXITCODE -ne 0) {
    Write-Host "离线安装失败（可能是 Python 版本与 wheel 缓存不匹配），尝试联网安装源码 ..." -ForegroundColor Yellow
    & $venvPy -m pip install (Join-Path $root 'ensp-mcp-1.3.0')
    if ($LASTEXITCODE -ne 0) { throw "ensp-mcp 安装失败" }
}

# 4. Host-Only 网卡（AR/WLAN 虚拟设备启动前提）
Write-Host ""
Write-Host "检查 VirtualBox Host-Only 网卡 ..." -ForegroundColor Yellow
Ensure-HostOnly

# 5. 环境自检
Write-Host ""
Write-Host "运行环境自检 ..." -ForegroundColor Yellow
& $venvPy -m ensp_mcp.doctor

# 6. 注册到 Claude Code（user 作用域）
# 优先用 npm 的 claude.cmd：PowerShell 直接调用 claude.ps1 时会吞掉分隔符“--”，
# 导致 -m 被 claude 当成自身选项而报 unknown option。
$claudeExe = $null
$c = Get-Command claude.cmd -ErrorAction SilentlyContinue
if ($c) {
    $claudeExe = $c.Source
} else {
    $c = Get-Command claude -ErrorAction SilentlyContinue
    if ($c -and $c.Source -notlike '*.ps1') { $claudeExe = $c.Source }
}

if ($claudeExe) {
    Write-Host ""
    Write-Host "注册到 Claude Code ..." -ForegroundColor Yellow
    # 未注册时 remove 会失败，忽略即可
    cmd /c "`"$claudeExe`" mcp remove --scope user ensp-mcp >nul 2>&1"
    cmd /c "`"$claudeExe`" mcp add --scope user ensp-mcp -- `"$venvPy`" -m ensp_mcp.server"
    if ($LASTEXITCODE -ne 0) { throw "claude mcp add 失败（exit $LASTEXITCODE）" }
    cmd /c "`"$claudeExe`" mcp list"
} else {
    Write-Host ""
    Write-Host "未找到 claude 命令，跳过自动注册。手动执行：" -ForegroundColor Yellow
    Write-Host "claude mcp add --scope user ensp-mcp -- `"$venvPy`" -m ensp_mcp.server"
}

# 6b. 注册到 WorkBuddy（~/.workbuddy/mcp.json），已有配置原样保留、只合并本服务
$wbMcp = Join-Path $env:USERPROFILE '.workbuddy\mcp.json'
try {
    $config = if (Test-Path $wbMcp) {
        Get-Content $wbMcp -Raw -Encoding UTF8 | ConvertFrom-Json
    } else {
        [pscustomobject]@{ mcpServers = [pscustomobject]@{} }
    }
    if (-not $config.PSObject.Properties['mcpServers']) {
        $config | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    $server = [pscustomobject]@{ command = $venvPy; args = @('-m', 'ensp_mcp.server') }
    if ($config.mcpServers.PSObject.Properties['ensp-mcp']) {
        $config.mcpServers.'ensp-mcp' = $server
    } else {
        $config.mcpServers | Add-Member -NotePropertyName 'ensp-mcp' -NotePropertyValue $server -Force
    }
    $wbDir = Split-Path $wbMcp
    if (-not (Test-Path $wbDir)) { New-Item -ItemType Directory -Path $wbDir -Force | Out-Null }
    $json = $config | ConvertTo-Json -Depth 10
    # 无 BOM 的 UTF-8，避免客户端把 BOM 当内容
    [System.IO.File]::WriteAllText($wbMcp, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host ""
    Write-Host "已注册到 WorkBuddy: $wbMcp（在连接器管理里信任 ensp-mcp 后生效）" -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "写入 WorkBuddy 配置失败：$($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "部署完成（ensp-mcp 1.3.0，19 个工具）。请重启 Claude Code / WorkBuddy 后使用。" -ForegroundColor Green
Write-Host "新增 device_batch（多设备并发）与 lab_verify（实验自动验收）两个工具。" -ForegroundColor Green
Write-Host "Host-Only 网卡已由脚本自动检查/创建；USG6000V、SVRP（NE40E）镜像未安装时仅对应型号不可用。" -ForegroundColor Green
