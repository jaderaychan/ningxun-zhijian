# 一键环境准备（Windows / PowerShell）
#
# 目标：让另一个人拿到这个仓库后，能用一条命令把环境装到可运行状态，
#       并看到 AGH 插件的确切安装位置与步骤。
#
# 用法：
#   pwsh -File scripts\setup.ps1
#   pwsh -File scripts\setup.ps1 -SkipPython     # 只复制插件
#
# 完成后会写出 .floodkit.json，AGH 工具插件据此找到 Python 解释器与依赖目录。

[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [string]$AghRepo = "",
    [switch]$SkipPython
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Write-Host "项目根目录: $root" -ForegroundColor Cyan

# ---------------------------------------------------------------- Python 依赖
$pylibs = Join-Path $root ".pylibs"
if (-not $SkipPython) {
    if (-not $PythonExe) {
        # 优先复用 DSH 自带的 Python 3.12（有 pip 且轮子齐全），其次找系统 python
        $candidates = @(
            (Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"),
            "python.exe",
            "python3.exe"
        )
        foreach ($candidate in $candidates) {
            try {
                $resolved = (Get-Command $candidate -ErrorAction Stop).Source
                & $resolved -c "import sys; assert sys.version_info[:2] == (3, 12)" 2>$null
                if ($LASTEXITCODE -eq 0) { $PythonExe = $resolved; break }
            } catch { }
        }
    }
    if (-not $PythonExe) { throw "找不到 Python 3.12。请用 -PythonExe 指定解释器路径。" }
    Write-Host "使用 Python: $PythonExe" -ForegroundColor Cyan

    # --target 安装会被 pip 从系统临时目录“移动”过来，Windows 上移动会保留源目录权限，
    # 导致后续受限进程读不到。这里把临时目录也放到项目内，避免继承错乱。
    $tmp = Join-Path $root ".piptmp"
    New-Item -ItemType Directory -Force -Path $pylibs, $tmp | Out-Null
    $env:TEMP = $tmp
    $env:TMP = $tmp

    Write-Host "安装科学计算依赖到 $pylibs ..." -ForegroundColor Cyan
    & $PythonExe -m pip install --no-cache-dir --upgrade --target $pylibs `
        --index-url https://pypi.tuna.tsinghua.edu.cn/simple `
        --trusted-host pypi.tuna.tsinghua.edu.cn `
        --progress-bar off numpy scipy matplotlib rasterio
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }
} else {
    Write-Host "跳过 Python 依赖安装" -ForegroundColor Yellow
}

# ------------------------------------------------------------ 写出本地配置
$config = [ordered]@{ python = $PythonExe; pylibs = $pylibs }
$configPath = Join-Path $root ".floodkit.json"
$config | ConvertTo-Json | Set-Content -Path $configPath -Encoding UTF8
Write-Host "已写出 $configPath" -ForegroundColor Green

# ------------------------------------------------------------ AGH 插件位置
Write-Host ""
Write-Host "=== AGH 插件安装 ===" -ForegroundColor Cyan
Write-Host "AGH 的 file: 包源必须是 ``file:./`` 开头的相对路径，且不允许 ``..``，"
Write-Host "所以插件必须先复制进 AGH 仓库目录。"
if ($AghRepo) {
    if (-not (Test-Path $AghRepo)) { throw "AGH 仓库不存在: $AghRepo" }
    $target = Join-Path $AghRepo "flood-tools"
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    Copy-Item (Join-Path $root "agh-plugin") $target -Recurse
    Write-Host "已复制插件到 $target" -ForegroundColor Green
    Write-Host ""
    Write-Host "下一步（CLI 需要 TTY，无法脚本化，必须走 Web 工作台）：" -ForegroundColor Yellow
    Write-Host "  1. 启动 AGH:  node `"$AghRepo\packages\cli\dist\local\agnes.mjs`" serve"
    Write-Host "  2. 打开工作台，进入 设置 → 插件管理"
    Write-Host "  3. 源填:  file:./flood-tools"
    Write-Host "  4. 核对来源与完整性哈希后，确认安装 → 信任 → 启用"
    Write-Host "  5. 看到 8 个工具:"
    Write-Host "       flood_selftest        解析校核（调用内核前先验算）"
    Write-Host "       flood_resolve_place   地名 → 研究区范围（只认南京，认不出就报错）"
    Write-Host "       flood_weather         查预报（三个模式的区间）"
    Write-Host "       flood_fetch_dem       拉取研究区 DEM 瓦片"
    Write-Host "       flood_run_event       跑一次完整降雨事件"
    Write-Host "       flood_publish_report  渲染交互式报告，并给出可打开的网址"
    Write-Host "       flood_verify_report   用 JavaScript 重算水量平衡"
    Write-Host "       flood_crosslang_check 跨语言独立复算 SCS-CN 与水位—库容反演"
} else {
    Write-Host "未指定 -AghRepo，跳过插件复制。示例：" -ForegroundColor Yellow
    Write-Host "  pwsh -File scripts\setup.ps1 -AghRepo 'D:\agh\agnes-harness'"
}

# ------------------------------------------------------------ 自检
Write-Host ""
Write-Host "=== 自检 ===" -ForegroundColor Cyan
$env:PYTHONPATH = "$pylibs;$root"
& $PythonExe -X utf8 -m floodkit selftest
if ($LASTEXITCODE -ne 0) { throw "floodkit 自检失败" }
Write-Host ""
Write-Host "环境准备完成。下一步：" -ForegroundColor Green
Write-Host "  $PythonExe -X utf8 -m floodkit samples --bounds 118.72 32.02 118.82 32.10"
