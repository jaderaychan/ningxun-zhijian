# 生成提交用 ZIP
#
# 规则要求（官方《参赛规程》）：
#   · 单个 ZIP 包，大小不超过 200MB
#   · ZIP 命名按照：组别-参赛编号-队伍名称-队长姓名
#   · 可包含：代码、安装包、运行说明、演示视频、测试样例
#
# 用法：
#   pwsh -File scripts\package-submission.ps1 `
#        -Group 本科生组 -EntryNumber A12345 -TeamName 宁讯智鉴 -CaptainName 张三
#
# 脚本会：
#   1. 只收集提交需要的文件（排除源码依赖、数据瓦片、缓存等体积大户）
#   2. 写出 MANIFEST.txt（含每个文件的 SHA256，便于评委核对完整性）
#   3. 报告最终体积，超过 200MB 直接失败而不是静默产出废包

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Group,        # 组别，例如 本科生组
    [Parameter(Mandatory = $true)][string]$EntryNumber,  # 参赛编号
    [Parameter(Mandatory = $true)][string]$TeamName,     # 队伍名称
    [Parameter(Mandatory = $true)][string]$CaptainName,  # 队长姓名
    [string]$OutDir = "",
    [string]$ExtraFile = ""                              # 可选：演示视频等附加文件
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $root "dist" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# 目录名里出现这些说明是依赖/缓存/原始数据，不该进提交包
$excludeDirs = @(
    "_mutation", "_cleanroom", "_zipview",
    "_agh_src", ".pnpm-store", ".pylibs", ".corepack", ".node-headers",
    ".npm-cache", ".pnpm-home", ".venv", ".agh-home", ".mplconfig",
    ".acl-recovery", ".piptmp", "agh_docs", "__pycache__", ".git",
    "dist", "_cleanroom"
)
# 原始数据瓦片体积很大，评委可用脚本自己重新下载
$excludeSubPaths = @("data\dem", "data\landcover", "data\osm", "data\tmp",
                     "data\node-headers", "data\weather")

$staging = Join-Path ([IO.Path]::GetTempPath()) ("floodkit-submit-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $staging | Out-Null

Write-Host "收集提交文件 …" -ForegroundColor Cyan
$collected = 0
$totalBytes = 0L
$manifest = New-Object System.Collections.Generic.List[string]

# 先算出文件清单再逐个复制：不要"整目录复制完再删"——那样会把被排除目录里的
# 受限文件也读一遍，既慢又可能直接失败（测试遗留的临时目录就曾卡在这里）。
# Derived caches must never ride along, whatever directory they end up in.
$excludeExtensions = @(".npy", ".part")
$excludePrefixes = @()
foreach ($rel in $excludeSubPaths) { $excludePrefixes += ($rel -replace '/', '\') }

$topLevel = Get-ChildItem -LiteralPath $root -Force | Where-Object { $excludeDirs -notcontains $_.Name }
foreach ($entry in $topLevel) {
    $files = if ($entry.PSIsContainer) {
        Get-ChildItem -LiteralPath $entry.FullName -Recurse -Force -File -ErrorAction SilentlyContinue
    } else {
        @($entry)
    }
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($root.Length + 1)
        $skip = $false
        foreach ($prefix in $excludePrefixes) {
            if ($relative.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { $skip = $true; break }
        }
        if ($skip) { continue }
        if ($relative -match '\\__pycache__\\' -or $file.Name -like '*.pyc') { continue }
        if ($excludeExtensions -contains $file.Extension.ToLowerInvariant()) { continue }

        $destPath = Join-Path $staging $relative
        $destDir = Split-Path -Parent $destPath
        if (-not (Test-Path -LiteralPath $destDir)) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
        Copy-Item -LiteralPath $file.FullName -Destination $destPath -Force
    }
}

if ($ExtraFile) {
    if (-not (Test-Path -LiteralPath $ExtraFile)) { throw "附加文件不存在: $ExtraFile" }
    Copy-Item -LiteralPath $ExtraFile -Destination (Join-Path $staging (Split-Path -Leaf $ExtraFile)) -Force
}

# 清单：相对路径 + 大小 + SHA256
Get-ChildItem -LiteralPath $staging -Recurse -Force -File | Sort-Object FullName | ForEach-Object {
    $rel = $_.FullName.Substring($staging.Length + 1)
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower()
    $script:totalBytes += $_.Length
    $script:collected += 1
    $script:manifest.Add(("{0}  {1,10}  sha256:{2}" -f $rel, $_.Length, $hash))
}
$manifest.Insert(0, "floodkit submission manifest — $collected files, $([math]::Round($totalBytes/1MB,2)) MB")
$manifest.Insert(1, "generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$manifest.Insert(2, "")
$manifest | Set-Content -LiteralPath (Join-Path $staging "MANIFEST.txt") -Encoding UTF8

# 命名严格按规则
$zipName = "$Group-$EntryNumber-$TeamName-$CaptainName.zip"
$zipPath = Join-Path $OutDir $zipName
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }

Write-Host "压缩 …" -ForegroundColor Cyan
Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $zipPath -CompressionLevel Optimal
Remove-Item -LiteralPath $staging -Recurse -Force

$zipBytes = (Get-Item -LiteralPath $zipPath).Length
$zipMB = [math]::Round($zipBytes / 1MB, 2)
Write-Host ""
Write-Host ("文件数 : {0}" -f $collected)
Write-Host ("原始   : {0} MB" -f [math]::Round($totalBytes / 1MB, 2))
Write-Host ("压缩后 : {0} MB" -f $zipMB)
Write-Host ("路径   : {0}" -f $zipPath)

if ($zipMB -gt 200) {
    throw "ZIP 超过 200MB 上限（$zipMB MB）。请用 -ExtraFile 移到仓库/共享链接，或进一步排除数据目录。"
}
Write-Host "符合 200MB 限制 ✔" -ForegroundColor Green
