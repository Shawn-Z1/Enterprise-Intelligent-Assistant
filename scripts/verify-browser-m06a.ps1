[CmdletBinding()]
param([string]$BaseUrl='http://127.0.0.1:8080',[string]$Session='zhixing-m06a-browser',[switch]$ReuseLoggedInSession)
$ErrorActionPreference='Stop'
# 本阶段只用隔离浏览器账号验证公开来源，不上传新知识或重复M06全套浏览器流程。
$projectRoot=Split-Path -Parent $PSScriptRoot
$outputPath=Join-Path $projectRoot 'verification/M06A'
New-Item -ItemType Directory -Force $outputPath | Out-Null
$browserSession=$Session
$checks=[Collections.Generic.List[object]]::new()

function Browser {
    <# 接收命令数组，操作本阶段隔离浏览器并返回安全输出；失败立即抛错。 #>
    param([string[]]$Arguments)
    $value=& npx --offline --yes agent-browser --session $browserSession @Arguments 2>&1
    if($LASTEXITCODE -ne 0){throw ($value -join "`n")}
    return $value -join "`n"
}
function Eval-Page {
    <# 接收JavaScript，返回浏览器求值结果；不读取Cookie或运行凭证。 #>
    param([string]$Expression)
    return (Browser @('eval','-b',[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Expression)))) | ConvertFrom-Json
}
function Check {
    <# 接收验收名称和页面条件，保存真实布尔结果；失败不继续声称通过。 #>
    param([string]$Name,[string]$Expression)
    $passed=Eval-Page $Expression
    $checks.Add(@{name=$Name;passed=[bool]$passed})
    if(-not $passed){throw "浏览器验收失败：$Name"}
    Write-Host "通过：$Name"
}

$configLines=Get-Content (Join-Path $projectRoot '.env')
$loginPassword=($configLines | Where-Object {$_ -match '^INITIAL_ADMIN_PASSWORD='}) -replace '^INITIAL_ADMIN_PASSWORD=',''
try {
    if(-not $ReuseLoggedInSession){
        $loginPassword | & npx --offline --yes agent-browser auth save $browserSession --url "$BaseUrl/login" --username admin --password-stdin --username-selector '#account' --password-selector '#password' --submit-selector '.primary-button' | Out-Null
        if($LASTEXITCODE -ne 0){throw '隔离登录配置失败'}
        Browser @('open',"$BaseUrl/login") | Out-Null
        Browser @('auth','login',$browserSession) | Out-Null
    }
    Browser @('set','viewport','1440','1000') | Out-Null
    Browser @('wait','--fn','document.querySelector(".profile-button")!==null','--timeout','30000') | Out-Null
    Browser @('snapshot','-i') | Set-Content (Join-Path $outputPath 'browser-start.txt')
    Browser @('fill','textarea','公开联网搜索阿里云CleverSee CNLiteBasic国内极速版官方价格，给可核实出处。') | Out-Null
    Browser @('find','role','button','click','--name','发送消息','--exact') | Out-Null
    Browser @('wait','--fn','document.querySelector(".message-sources button")!==null && document.querySelector(".chat-run-state.completed")!==null','--timeout','180000') | Out-Null
    Check '真实搜索回答包含公开来源' 'document.querySelector(".message-sources")?.textContent.includes("公开")===true'
    Browser @('click','.message-sources button') | Out-Null
    Browser @('wait','--fn','document.querySelector(".source-excerpt")!==null','--timeout','15000') | Out-Null
    Check '安全文本预览与时间' 'document.querySelector(".source-panel")?.textContent.includes("获取时间") && !document.querySelector(".source-panel iframe,.source-panel script,.source-panel img")'
    Check '公开原网页链接' 'document.querySelector(".source-panel a")?.getAttribute("rel")==="noopener noreferrer"'
    Browser @('screenshot',(Join-Path $outputPath 'source-desktop.png')) | Out-Null
    Browser @('set','viewport','375','812') | Out-Null
    Check '窄屏无横向溢出' 'document.documentElement.scrollWidth<=window.innerWidth+1'
    Browser @('screenshot',(Join-Path $outputPath 'source-mobile.png')) | Out-Null
    Browser @('press','Escape') | Out-Null
    Check 'Escape关闭并恢复来源焦点' '!document.querySelector(".source-panel") && document.activeElement?.closest(".message-sources")!==null'
    Browser @('reload') | Out-Null
    Browser @('wait','--fn','document.querySelector(".message-sources button")!==null','--timeout','30000') | Out-Null
    Check '刷新保留来源和原回答' 'document.querySelector(".message-sources")?.textContent.includes("公开")===true'
    Browser @('snapshot','-i') | Set-Content (Join-Path $outputPath 'browser-final.txt')
    Eval-Page '(async()=>{const rows=await(await fetch("/api/conversations")).json();const latest=rows[0];const detail=await(await fetch("/api/conversations/"+latest.conversation_id)).json();return {conversation_id:latest.conversation_id,run_id:detail.last_run?.run_id,trace_id:detail.last_run?.trace_id};})()' | ConvertTo-Json | Set-Content (Join-Path $outputPath 'browser-trace.json')
    Browser @('find','role','button','click','--name','退出登录','--exact') | Out-Null
} finally {
    $checks | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $outputPath 'browser-checks.json')
    & npx --offline --yes agent-browser --session $browserSession close | Out-Null
    if(-not $ReuseLoggedInSession){& npx --offline --yes agent-browser auth delete $browserSession | Out-Null}
}
