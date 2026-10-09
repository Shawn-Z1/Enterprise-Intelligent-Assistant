[CmdletBinding()]
param([string]$BaseUrl='http://127.0.0.1:8080')
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$outputDirectory=Join-Path $projectRoot 'verification/M04'
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
$checks=[Collections.Generic.List[object]]::new()
$memberSession='zhixing-m04-member'
$otherSession='zhixing-m04-other'
$tag='M04验收-'+[Guid]::NewGuid().ToString('N').Substring(0,8)

function Invoke-Browser {
    <# 接收本次测试会话与 CLI 参数，返回实际输出；失败中止，不记录认证凭证。 #>
    param([string]$Session,[string[]]$Arguments)
    $output=& npx --yes agent-browser --session $Session @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw ($output -join "`n") }
    return ($output -join "`n")
}

function Assert-Page {
    <# 接收会话名、检查名和 JS 表达式，将无凭证证据保存到忽略目录，失败即停止。 #>
    param([string]$Session,[string]$Name,[string]$Expression)
    $encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Expression))
    $result=(Invoke-Browser $Session @('eval','-b',$encoded)) | ConvertFrom-Json
    $checks.Add([pscustomobject]@{name=$Name;passed=($result.passed -eq $true);evidence=$result})
    $checks | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $outputDirectory 'browser-checks.json') -Encoding utf8
    if ($result.passed -ne $true) { throw "浏览器验收失败：$Name" }
    Write-Host "通过：$Name"
}

Push-Location $projectRoot
try {
    $lines=Get-Content -LiteralPath '.env'
    $password=($lines | Where-Object { $_ -match '^INITIAL_DEMO_PASSWORD=' }) -replace '^INITIAL_DEMO_PASSWORD=',''
    foreach ($profile in @(@($memberSession,'zhangsan'),@($otherSession,'lisi'))) {
        $password | & npx --yes agent-browser auth save $profile[0] --url "$BaseUrl/login" --username $profile[1] --password-stdin --username-selector '#account' --password-selector '#password' --submit-selector '.primary-button'
        if ($LASTEXITCODE -ne 0) { throw '认证凭证库初始化失败' }
        & npx --yes agent-browser --session $profile[0] open "$BaseUrl/login"
        Invoke-Browser $profile[0] @('set','viewport','1440','900') | Out-Null
        Invoke-Browser $profile[0] @('cookies','clear') | Out-Null
        Invoke-Browser $profile[0] @('auth','login',$profile[0]) | Out-Null
        Invoke-Browser $profile[0] @('wait','--fn','document.querySelector(".chat-workspace") !== null') | Out-Null
    }
    Invoke-Browser $memberSession @('find','role','button','click','--name','新建对话','--exact') | Out-Null
    Assert-Page $memberSession '真实登录后普通文字输入开放' '({passed: !document.querySelector("#message").disabled && document.body.textContent.includes("北京时间")})'
    Invoke-Browser $memberSession @('eval','window.__m04Samples=[]; window.__m04Observer=new MutationObserver(()=>{const node=document.querySelector(".live-reply .message-content");const size=node?.textContent.length||0;if(size>0 && window.__m04Samples.at(-1)!==size)window.__m04Samples.push(size);});window.__m04Observer.observe(document.body,{subtree:true,childList:true,characterData:true});') | Out-Null
    $prompt="请记住本会话代号是$tag。然后用中文说明北京时间与 UTC 的关系，写八条，每条约七十个汉字。最后一行只写这个代号。"
    Invoke-Browser $memberSession @('fill','#message',$prompt) | Out-Null
    Invoke-Browser $memberSession @('find','role','button','click','--name','发送消息','--exact') | Out-Null
    Invoke-Browser $memberSession @('wait','--fn','document.querySelector(".chat-run-state.completed") !== null') | Out-Null
    Assert-Page $memberSession '真实回答在完成前多次增量展示' '({passed:window.__m04Samples.length>1,sampleCount:window.__m04Samples.length,lengths:window.__m04Samples.slice(0,8)})'
    Assert-Page $memberSession '真实模型回复显示且不展示型号与计量' ('({passed: document.querySelectorAll(".chat-message.assistant").length===1 && document.querySelector(".chat-message.assistant").textContent.includes("'+$tag+'") && !document.querySelector(".message-footnote")})')
    Assert-Page $memberSession '历史目录与独立会话路径已保存' '({passed: location.pathname.startsWith("/conversations/") && document.querySelectorAll(".history-item").length>0,path:location.pathname})'
    $conversationPath=((Invoke-Browser $memberSession @('get','url')) -replace [regex]::Escape($BaseUrl),'').Trim()
    Invoke-Browser $memberSession @('screenshot',(Join-Path $outputDirectory 'chat-desktop.png')) | Out-Null
    Invoke-Browser $memberSession @('open',($BaseUrl+$conversationPath)) | Out-Null
    Invoke-Browser $memberSession @('wait','--fn','document.querySelectorAll(".chat-message.assistant").length===1') | Out-Null
    Assert-Page $memberSession '刷新后原消息与会话路径恢复' '({passed:document.querySelectorAll(".chat-message.user").length===1 && document.querySelectorAll(".chat-message.assistant").length===1 && location.pathname.startsWith("/conversations/")})'
    Invoke-Browser $memberSession @('fill','#message','本会话的代号是什么？请仅输出代号。') | Out-Null
    Invoke-Browser $memberSession @('find','role','button','click','--name','发送消息','--exact') | Out-Null
    Invoke-Browser $memberSession @('wait','--fn','document.querySelectorAll(".chat-message.assistant").length===2 && document.querySelector(".chat-run-state.completed") !== null') | Out-Null
    Assert-Page $memberSession '真实多轮回答使用此前本人上下文' ('({passed:document.querySelectorAll(".chat-message.assistant")[1].textContent.includes("'+$tag+'")})')
    Invoke-Browser $memberSession @('find','role','button','click','--name','重命名会话','--exact') | Out-Null
    Invoke-Browser $memberSession @('fill','#conversation-title',('第四阶段验收 · '+$tag)) | Out-Null
    Invoke-Browser $memberSession @('find','role','button','click','--name','保存','--exact') | Out-Null
    Invoke-Browser $memberSession @('wait','--fn','document.querySelector(".rename-form") === null') | Out-Null
    Assert-Page $memberSession '重命名同步历史目录' ('({passed:document.querySelector(".chat-heading h1").textContent==="第四阶段验收 · '+$tag+'" && Array.from(document.querySelectorAll(".history-item")).some(item=>item.textContent.includes("'+$tag+'"))})')
    Invoke-Browser $memberSession @('fill','input[aria-label="搜索历史会话"]',$tag) | Out-Null
    Assert-Page $memberSession '历史标题搜索有效' '({passed:document.querySelectorAll(".history-item").length===1})'
    Assert-Page $otherSession '第二用户历史目录不混入本人会话' ('(async()=>{const rows=await(await fetch("/api/conversations")).json();return {passed:!rows.some(row=>row.title.includes("'+$tag+'"))};})()')
    Assert-Page $otherSession '第二用户猜测会话路径仍被后台拒绝' ('(async()=>{const response=await fetch("/api'+$conversationPath+'");return {passed:response.status===404,status:response.status};})()')
    foreach ($size in @(@(1440,900),@(1366,768),@(768,1024),@(375,812))) {
        Invoke-Browser $memberSession @('set','viewport',[string]$size[0],[string]$size[1]) | Out-Null
        Assert-Page $memberSession "$($size[0])px 聊天页无横向溢出且输入区可见" '(()=>{const box=document.querySelector(".chat-composer-area").getBoundingClientRect();return {passed:document.documentElement.scrollWidth<=innerWidth && box.bottom<=innerHeight && box.top>=0,width:innerWidth,composerBottom:box.bottom,height:innerHeight};})()'
    }
    Invoke-Browser $memberSession @('screenshot',(Join-Path $outputDirectory 'chat-mobile.png')) | Out-Null
    Invoke-Browser $memberSession @('set','viewport','1440','900') | Out-Null
    Invoke-Browser $memberSession @('fill','#message','请连续写五十条较长的工作建议，每条约五十个汉字。') | Out-Null
    Invoke-Browser $memberSession @('find','role','button','click','--name','发送消息','--exact') | Out-Null
    Invoke-Browser $memberSession @('wait','--fn','document.querySelector("button[aria-label=\"停止回答\"]") !== null') | Out-Null
    Invoke-Browser $memberSession @('find','role','button','click','--name','停止回答','--exact') | Out-Null
    Invoke-Browser $memberSession @('wait','--fn','document.querySelector(".chat-run-state.cancelled") !== null') | Out-Null
    Assert-Page $memberSession '停止按钮结束真实后台任务' '(async()=>{const detail=await(await fetch("/api"+location.pathname)).json();return {passed:document.querySelector(".chat-run-state.cancelled")!==null && detail.last_run.status==="cancelled" && detail.messages.filter(item=>item.role==="assistant").length===2,status:detail.last_run.status,savedAssistantMessages:detail.messages.filter(item=>item.role==="assistant").length};})()'
    Assert-Page $memberSession '会话接口所有时间为北京时间偏移' ('(async()=>{const row=await(await fetch("/api'+$conversationPath+'")).json();return {passed:row.messages.every(item=>item.created_at.endsWith("+08:00"))};})()')
    foreach ($session in @($memberSession,$otherSession)) {
        Invoke-Browser $session @('errors') | Set-Content -LiteralPath (Join-Path $outputDirectory "$session-errors.txt") -Encoding utf8
        Invoke-Browser $session @('find','role','button','click','--name','退出登录') | Out-Null
        Invoke-Browser $session @('close') | Out-Null
        & npx --yes agent-browser auth delete $session | Out-Null
    }
    & npx --yes agent-browser close | Out-Null
    Write-Host "浏览器 M04 验收完成，保留本人原始验收会话：$tag"
} finally { Pop-Location }
