[CmdletBinding()]
param([string]$BaseUrl = 'http://localhost:8080')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$evidencePath = Join-Path $projectRoot 'verification/M01/browser-checks.json'
# 验收产物不随仓库分发，独立运行脚本时先创建本机输出目录。
New-Item -ItemType Directory -Path (Split-Path -Parent $evidencePath) -Force | Out-Null
# 所有检查使用真实 Chromium 页面，不拦截请求，也不注入假 API 响应。
$checks = [System.Collections.Generic.List[object]]::new()
$session = 'zhixing-m01'

function Invoke-Browser {
    <# 接收 CLI 参数数组，运行独立验收会话；返回实际输出，失败则停止。 #>
    param([string[]]$BrowserArguments)
    $output = & npx --yes agent-browser --session $session @BrowserArguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw ($output -join "`n") }
    return ($output -join "`n")
}

function Assert-Page {
    <# 接收检查名称和返回对象的 JS 表达式；记录证据，passed 不为 true 即失败。 #>
    param([string]$Name, [string]$Expression)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Expression))
    $result = (Invoke-Browser @('eval', '-b', $encoded)) | ConvertFrom-Json
    $checks.Add([pscustomobject]@{name=$Name; passed=($result.passed -eq $true); evidence=$result})
    $checks | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $evidencePath -Encoding utf8
    if ($result.passed -ne $true) { throw "浏览器验收失败：$Name" }
    Write-Host "通过：$Name"
}

Push-Location $projectRoot
try {
    # 首次启动浏览器时直接输出，避免后台进程继承捕获管道使调用一直等待。
    & npx --yes agent-browser --session $session open "$BaseUrl/login"
    if ($LASTEXITCODE -ne 0) { throw '浏览器会话启动失败' }
    Invoke-Browser @('set', 'viewport', '1440', '900') | Out-Null
    # 等待真实请求结果而非页面下方元素的可见性，避免默认短窗口导致等待停滞。
    Invoke-Browser @('wait', '--fn', 'document.querySelector(".login-status")?.textContent.includes("基础服务已就绪") === true') | Out-Null
    Assert-Page '登录页面真实预览状态' '({passed: document.querySelector(".primary-button").disabled && document.querySelector("#password").type === "password", title: document.title})'
    Assert-Page '登录左右比例与文字可读性' '({passed: Math.abs(document.querySelector(".login-story").getBoundingClientRect().width / document.querySelector(".login-form-area").getBoundingClientRect().width - 1.5) < 0.02 && parseFloat(getComputedStyle(document.querySelector(".story-note")).fontSize) >= 16 && parseFloat(getComputedStyle(document.querySelector("#account")).fontSize) >= 17, securityText: getComputedStyle(document.querySelector(".story-note")).fontSize, inputText: getComputedStyle(document.querySelector("#account")).fontSize, formWidth: document.querySelector(".login-form-wrap").getBoundingClientRect().width})'
    Assert-Page '登录辅助文字至少14px' '(() => { const small = Array.from(document.querySelectorAll("body *")).filter(el => el instanceof HTMLElement && el.getClientRects().length && getComputedStyle(el).visibility !== "hidden" && Array.from(el.childNodes).some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim()) && parseFloat(getComputedStyle(el).fontSize) < 14).map(el => ({text: el.textContent, size: getComputedStyle(el).fontSize})); return {passed: small.length === 0, small}; })()'
    Invoke-Browser @('fill', '#password', 'M01-demo-only') | Out-Null
    Invoke-Browser @('find', 'role', 'button', 'click', '--name', '显示密码') | Out-Null
    Assert-Page '密码显示' '({passed: document.querySelector("#password").type === "text"})'
    Invoke-Browser @('find', 'role', 'button', 'click', '--name', '隐藏密码') | Out-Null
    Assert-Page '密码隐藏' '({passed: document.querySelector("#password").type === "password"})'
    Invoke-Browser @('find', 'role', 'button', 'click', '--name', '查看注册页面') | Out-Null
    Assert-Page '注册不会假成功' '({passed: document.querySelector(".primary-button").disabled && document.querySelector(".primary-button").textContent.includes("注册暂未开放")})'
    Invoke-Browser @('find', 'role', 'button', 'click', '--name', '查看登录页面') | Out-Null
    Invoke-Browser @('fill', '#password', '') | Out-Null
    Invoke-Browser @('screenshot', 'verification/M01/login-desktop.png') | Out-Null
    Invoke-Browser @('set', 'viewport', '375', '812') | Out-Null
    Assert-Page '375px 登录布局无横向溢出' '({passed: document.documentElement.scrollWidth <= innerWidth, width: innerWidth, scrollWidth: document.documentElement.scrollWidth})'
    Invoke-Browser @('screenshot', '--full', 'verification/M01/login-mobile.png') | Out-Null
    # 大字号手机登录页需要滚动，先让入口进入视口，再点击并等待目标页面。
    Invoke-Browser @('scrollintoview', '.preview-link') | Out-Null
    Invoke-Browser @('click', '.preview-link') | Out-Null
    Invoke-Browser @('wait', '--text', '模型会话将在 M04') | Out-Null
    Assert-Page '工作台访客与模型边界' '({passed: document.querySelector("#message").disabled && document.body.textContent.includes("尚未登录") && document.body.textContent.includes("模型会话将在 M04"), pathname: location.pathname})'
    foreach ($size in @(@(1440,900), @(1366,768), @(1024,768), @(768,1024), @(375,812))) {
        Invoke-Browser @('set', 'viewport', [string]$size[0], [string]$size[1]) | Out-Null
        Assert-Page "$($size[0])px 工作台无横向溢出" '({passed: document.documentElement.scrollWidth <= innerWidth, width: innerWidth, scrollWidth: document.documentElement.scrollWidth})'
        Assert-Page "$($size[0])px 输入区域保持可见" '({passed: document.querySelector("#message").getBoundingClientRect().bottom <= innerHeight && document.querySelector(".workspace-footer").getBoundingClientRect().bottom <= innerHeight + 1 && parseFloat(getComputedStyle(document.querySelector("#message")).fontSize) >= 17, inputBottom: document.querySelector("#message").getBoundingClientRect().bottom, height: innerHeight, textSize: getComputedStyle(document.querySelector("#message")).fontSize})'
        if ($size[0] -eq 1440) { Invoke-Browser @('screenshot', 'verification/M01/workspace-desktop.png') | Out-Null }
        if ($size[0] -eq 375) { Invoke-Browser @('screenshot', 'verification/M01/workspace-mobile.png') | Out-Null }
    }
    Assert-Page '工作台辅助文字至少14px' '(() => { const small = Array.from(document.querySelectorAll("body *")).filter(el => el instanceof HTMLElement && el.getClientRects().length && getComputedStyle(el).visibility !== "hidden" && Array.from(el.childNodes).some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim()) && parseFloat(getComputedStyle(el).fontSize) < 14).map(el => ({text: el.textContent, size: getComputedStyle(el).fontSize})); return {passed: small.length === 0, small}; })()'
    Assert-Page '小屏收起导航不可聚焦' '({passed: getComputedStyle(document.querySelector(".sidebar")).visibility === "hidden"})'
    Invoke-Browser @('find', 'role', 'button', 'click', '--name', '展开导航') | Out-Null
    Assert-Page '小屏导航打开' '({passed: getComputedStyle(document.querySelector(".sidebar")).visibility === "visible"})'
    Invoke-Browser @('find', 'role', 'button', 'click', '--name', '知识库管理') | Out-Null
    Assert-Page '知识入口保留且导航收起' '({passed: document.querySelector("main").textContent.includes("M05") && getComputedStyle(document.querySelector(".sidebar")).visibility === "hidden"})'
    Invoke-Browser @('find', 'role', 'button', 'click', '--name', '展开导航') | Out-Null
    Invoke-Browser @('find', 'role', 'button', 'click', '--name', '自动化任务流') | Out-Null
    Assert-Page '自动化仅说明后续开放' '({passed: document.querySelector("main").textContent.includes("后续版本开放") && document.querySelector("main").querySelectorAll("input,button").length === 0})'
    Invoke-Browser @('set', 'viewport', '1440', '900') | Out-Null
    Invoke-Browser @('find', 'role', 'button', 'click', '--name', '运行状态') | Out-Null
    Invoke-Browser @('wait', '--fn', 'document.querySelectorAll(".event-list strong").length > 0') | Out-Null
    Assert-Page '真实运行面板与事件' '({passed: document.querySelectorAll(".dependency").length === 4 && Array.from(document.querySelectorAll(".dependency")).every(item => item.textContent.includes("已连通")) && document.querySelector(".status-panel").textContent.includes("永久审计已存入 PostgreSQL"), dependencies: Array.from(document.querySelectorAll(".dependency")).map(item => item.textContent), events: document.querySelectorAll(".event-list strong").length})'
    Invoke-Browser @('screenshot', 'verification/M01/runtime-desktop.png') | Out-Null
    Invoke-Browser @('find', 'role', 'button', 'click', '--name', '关闭运行状态') | Out-Null
    Invoke-Browser @('find', 'role', 'button', 'click', '--name', '对话工作台') | Out-Null
    Invoke-Browser @('set', 'media', 'light', 'reduced-motion') | Out-Null
    Assert-Page '减少动画偏好' '({passed: matchMedia("(prefers-reduced-motion: reduce)").matches})'
    Invoke-Browser @('errors') | Set-Content -LiteralPath 'verification/M01/browser-errors.txt' -Encoding utf8
    Invoke-Browser @('snapshot', '-i') | Set-Content -LiteralPath 'verification/M01/browser-snapshot.txt' -Encoding utf8
    Write-Host '浏览器验收完成，见 verification/M01/browser-checks.json'
} finally { Pop-Location }
