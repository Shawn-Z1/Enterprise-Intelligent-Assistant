[CmdletBinding()]
param([string]$BaseUrl = 'http://127.0.0.1:8080')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$outputDirectory = Join-Path $projectRoot 'verification/M03'
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
# 仅创建本次测试账号和三个隔离浏览器会话；实际 API 不做任何 mock。
$checks = [Collections.Generic.List[object]]::new()
$testUsername = 'm03_browser_' + [Guid]::NewGuid().ToString('N').Substring(0,8)
$adminSession = 'zhixing-m03-admin'
$memberSession = 'zhixing-m03-member'
$newSession = 'zhixing-m03-new'

function Invoke-Browser {
    <# 接收会话名与 CLI 参数，返回实际输出；失败中止，不输出凭证。 #>
    param([string]$Session,[string[]]$Arguments)
    $output = & npx --yes agent-browser --session $Session @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw ($output -join "`n") }
    return ($output -join "`n")
}

function Assert-Page {
    <# 接收会话、检查名与 JS 表达式，只保存无凭证的验证结果，失败即中止。 #>
    param([string]$Session,[string]$Name,[string]$Expression)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Expression))
    $result = (Invoke-Browser $Session @('eval','-b',$encoded)) | ConvertFrom-Json
    $checks.Add([pscustomobject]@{name=$Name;passed=($result.passed -eq $true);evidence=$result})
    $checks | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $outputDirectory 'browser-checks.json') -Encoding utf8
    if ($result.passed -ne $true) { throw "页面验收失败：$Name" }
    Write-Host "通过：$Name"
}

Push-Location $projectRoot
try {
    $environmentLines = Get-Content -LiteralPath '.env'
    foreach ($profile in @(@($adminSession,'admin','INITIAL_ADMIN_PASSWORD'),@($memberSession,'zhangsan','INITIAL_DEMO_PASSWORD'))) {
        $password = ($environmentLines | Where-Object { $_ -match ('^'+$profile[2]+'=') }) -replace ('^'+$profile[2]+'='),''
        $password | & npx --yes agent-browser auth save $profile[0] --url "$BaseUrl/login" --username $profile[1] --password-stdin --username-selector '#account' --password-selector '#password' --submit-selector '.primary-button'
        if ($LASTEXITCODE -ne 0) { throw '安全凭证库初始化失败' }
        # 首次启动不捕获输出，避免 Windows 后台进程继承管道造成阻塞。
        & npx --yes agent-browser --session $profile[0] open "$BaseUrl/login"
        Invoke-Browser $profile[0] @('set','viewport','1440','900') | Out-Null
        Invoke-Browser $profile[0] @('cookies','clear') | Out-Null
        Invoke-Browser $profile[0] @('auth','login',$profile[0]) | Out-Null
        Invoke-Browser $profile[0] @('wait','--fn','document.querySelector(".profile-button") !== null') | Out-Null
    }
    Assert-Page $adminSession '管理员真实登录及专属菜单' '({passed: document.body.textContent.includes("系统管理员") && Array.from(document.querySelectorAll("nav button")).some(x => x.textContent === "职员管理")})'
    Assert-Page $memberSession '普通成员真实登录且无管理员菜单' '({passed: document.querySelector(".profile-button").textContent.includes("张三") && !Array.from(document.querySelectorAll("nav button")).some(x => x.textContent === "职员管理" || x.textContent === "调试与审计")})'
    Assert-Page $memberSession 'HttpOnly 会话不暴露给前端脚本' '({passed: !document.cookie.includes("zhixing_session=") && document.cookie.includes("zhixing_csrf=")})'
    Invoke-Browser $memberSession @('open',"$BaseUrl/") | Out-Null
    Invoke-Browser $memberSession @('wait','--fn','document.querySelector(".profile-button") !== null') | Out-Null
    Assert-Page $memberSession '刷新恢复本人会话' '({passed: document.querySelector(".profile-button").textContent.includes("张三")})'
    Invoke-Browser $memberSession @('click','.profile-button') | Out-Null
    Invoke-Browser $memberSession @('wait','--fn','document.querySelector(".profile-details") !== null') | Out-Null
    Assert-Page $memberSession '个人中心可信部门职级岗位' '({passed: document.querySelector("main").textContent.includes("技术部") && document.querySelector("main").textContent.includes("开发工程师") && document.querySelector("main").textContent.includes("普通成员")})'

    & npx --yes agent-browser --session $newSession open "$BaseUrl/login"
    Invoke-Browser $newSession @('set','viewport','1440','900') | Out-Null
    Invoke-Browser $newSession @('cookies','clear') | Out-Null
    Invoke-Browser $newSession @('open',"$BaseUrl/login") | Out-Null
    Invoke-Browser $newSession @('find','role','button','click','--name','注册普通账号') | Out-Null
    Invoke-Browser $newSession @('fill','#account',$testUsername) | Out-Null
    Invoke-Browser $newSession @('fill','#register-name','浏览器验收成员') | Out-Null
    # 密码仅通过标准输入送到浏览器，避免出现在命令行或验证输出。
    $bytes = New-Object byte[] 24
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    $testPassword = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    $fillScript = "document.querySelector('#password').value='$testPassword'; true"
    $fillScript | & npx --yes agent-browser --session $newSession eval --stdin | Out-Null
    Invoke-Browser $newSession @('scrollintoview','.primary-button') | Out-Null
    Invoke-Browser $newSession @('click','.primary-button') | Out-Null
    Invoke-Browser $newSession @('wait','--fn','document.querySelector(".profile-button") !== null') | Out-Null
    Assert-Page $newSession '真实注册进入零权限工作台' '({passed: document.querySelector(".profile-button").textContent.includes("浏览器验收成员") && document.body.textContent.includes("待分配部门") && !document.body.textContent.includes("职员管理")})'
    Invoke-Browser $newSession @('find','role','button','click','--name','知识库管理') | Out-Null
    Assert-Page $newSession '未授权知识页面明确拒绝' '({passed: document.querySelector("main").textContent.includes("尚未获得知识库权限") && document.querySelector("main").querySelectorAll("button,input").length === 0})'
    Assert-Page $newSession '真实普通浏览器不能访问管理 API' '(async () => { const response = await fetch("/api/admin/users"); return {passed:response.status===403,status:response.status}; })()'
    Assert-Page $newSession '知识和 SQL 请求在后台先拒绝' '(async () => { const csrf = document.cookie.split("; ").find(x => x.startsWith("zhixing_csrf=")).split("=")[1]; const results = await Promise.all(["search_knowledge","query_business_data"].map(name => fetch("/api/tools/"+name,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify({question:"验收权限"})}))); return {passed:results.every(x=>x.status===403),statuses:results.map(x=>x.status)}; })()'
    Invoke-Browser $newSession @('screenshot', (Join-Path $outputDirectory 'unassigned-member.png')) | Out-Null

    Invoke-Browser $adminSession @('find','role','button','click','--name','职员管理') | Out-Null
    Invoke-Browser $adminSession @('find','role','button','click','--name','刷新列表') | Out-Null
    Invoke-Browser $adminSession @('wait','--fn',"document.body.textContent.includes('$testUsername')") | Out-Null
    Invoke-Browser $adminSession @('find','role','button','click','--name',"管理 $testUsername") | Out-Null
    Invoke-Browser $adminSession @('select','#staff-department','D2') | Out-Null
    Invoke-Browser $adminSession @('select','#staff-grade','3') | Out-Null
    Invoke-Browser $adminSession @('fill','#staff-position','验收工程师') | Out-Null
    Invoke-Browser $adminSession @('check','#staff-business') | Out-Null
    Invoke-Browser $adminSession @('find','role','button','click','--name','保存分配') | Out-Null
    Invoke-Browser $adminSession @('wait','--text','职员资料与权限已保存') | Out-Null
    Assert-Page $adminSession '管理员页面实际赋权成功' ('(async () => { const rows=await (await fetch("/api/admin/users")).json(); const user=rows.find(x=>x.username==="'+$testUsername+'"); return {passed:user.grade===3 && user.business_query_enabled===true && user.role==="member",grade:user.grade,role:user.role}; })()')
    Invoke-Browser $adminSession @('screenshot',(Join-Path $outputDirectory 'staff-desktop.png')) | Out-Null
    Invoke-Browser $newSession @('find','role','button','click','--name','刷新资料') | Out-Null
    Invoke-Browser $newSession @('wait','--fn','document.querySelector("main").textContent.includes("已获得知识使用资格")') | Out-Null
    Assert-Page $newSession '原会话刷新立即显示已授权状态' '(async () => { const user=await (await fetch("/api/auth/me")).json(); return {passed:user.can_use_knowledge && user.can_query_business && user.role==="member",grade:user.grade}; })()'
    Invoke-Browser $newSession @('click','.profile-button') | Out-Null
    Invoke-Browser $newSession @('wait','--fn','document.querySelector(".profile-details") !== null') | Out-Null
    Assert-Page $newSession '原会话本人资料显示新岗位' '({passed: document.querySelector("main").textContent.includes("验收工程师")})'
    Invoke-Browser $newSession @('screenshot',(Join-Path $outputDirectory 'member-profile.png')) | Out-Null
    foreach ($size in @(@(1440,900),@(1366,768),@(768,1024),@(375,812))) {
        Invoke-Browser $newSession @('set','viewport',[string]$size[0],[string]$size[1]) | Out-Null
        Assert-Page $newSession "$($size[0])px 本人页面无横向溢出" '({passed: document.documentElement.scrollWidth <= innerWidth,width:innerWidth,scrollWidth:document.documentElement.scrollWidth})'
    }
    Invoke-Browser $newSession @('screenshot',(Join-Path $outputDirectory 'profile-mobile.png')) | Out-Null
    Invoke-Browser $adminSession @('find','role','button','click','--name',"管理 $testUsername") | Out-Null
    Invoke-Browser $adminSession @('select','#staff-department','') | Out-Null
    Invoke-Browser $adminSession @('find','role','button','click','--name','保存分配') | Out-Null
    Invoke-Browser $adminSession @('wait','--text','职员资料与权限已保存') | Out-Null
    Invoke-Browser $newSession @('find','role','button','click','--name','刷新资料') | Out-Null
    Assert-Page $newSession '旧 Cookie 撤权后立即失去资格' '(async () => { const user=await (await fetch("/api/auth/me")).json(); return {passed: !user.can_use_knowledge && !user.can_query_business && user.grade===null,grade:user.grade}; })()'
    Invoke-Browser $newSession @('find','role','button','click','--name','退出登录') | Out-Null
    Invoke-Browser $newSession @('wait','--fn','document.querySelector("#account") !== null') | Out-Null
    Assert-Page $newSession '真实退出后受保护资料不可访问' '(async () => { const response=await fetch("/api/auth/me"); return {passed:response.status===401,status:response.status}; })()'
    foreach ($session in @($adminSession,$memberSession)) { Invoke-Browser $session @('find','role','button','click','--name','退出登录') | Out-Null }
    # 清理创建的安全凭证库条目和测试浏览器，不输出任何 Cookie。
    foreach ($session in @($adminSession,$memberSession,$newSession)) { Invoke-Browser $session @('errors') | Set-Content -LiteralPath (Join-Path $outputDirectory "$session-errors.txt") -Encoding utf8; Invoke-Browser $session @('close') | Out-Null }
    foreach ($profile in @($adminSession,$memberSession)) { & npx --yes agent-browser auth delete $profile | Out-Null }
    & docker compose --profile tools run --rm --no-deps --entrypoint python -e PYTHONPATH=/app -v "${projectRoot}/backend/tests:/tests:ro" db-admin /tests/cleanup_m03_browser.py $testUsername
    if ($LASTEXITCODE -ne 0) { throw '测试账号清理失败，请检查保留的测试账号状态' }
    Write-Host "浏览器 M03 验收完成：$testUsername（零权限测试成员）"
} finally { Pop-Location }
