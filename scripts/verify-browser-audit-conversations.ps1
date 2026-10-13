[CmdletBinding()]
param([string]$BaseUrl='http://127.0.0.1:8080')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$output=Join-Path $root 'verification/audit-conversations'
New-Item -ItemType Directory -Force $output | Out-Null
$checks=[Collections.Generic.List[object]]::new()
# npx首次安装后定位官方CLI原生程序；文件输出避免Windows守护进程继承管道使脚本等待不结束。
$browserExecutable=Get-ChildItem -LiteralPath (Join-Path $env:LOCALAPPDATA 'npm-cache/_npx') -Directory |
    ForEach-Object {Join-Path $_.FullName 'node_modules/agent-browser/bin/agent-browser-win32-x64.exe'} |
    Where-Object {Test-Path -LiteralPath $_} | Select-Object -First 1
if(-not $browserExecutable){throw '请先运行npx --yes agent-browser --version安装CLI'}

function Browser {
    <# 接收会话和浏览器命令，返回结果；认证密码只通过stdin传递。 #>
    param([string]$Session,[string[]]$Arguments)
    Write-Host "[浏览器] $Session $($Arguments[0])"
    $tag=[Guid]::NewGuid().ToString('N')
    $stdout=Join-Path $output ($tag+'.stdout');$stderr=Join-Path $output ($tag+'.stderr')
    $quoted=@('--session',$Session)+$Arguments | ForEach-Object {'"'+$_.Replace('"','\"')+'"'}
    $process=Start-Process -FilePath $browserExecutable -ArgumentList ($quoted -join ' ') -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    if(-not $process.WaitForExit(60000)){$process.Kill();throw '浏览器CLI超过60秒，请检查会话'}
    $result=Get-Content -LiteralPath $stdout -Raw
    $errors=Get-Content -LiteralPath $stderr -Raw
    if($process.ExitCode -ne 0){throw $errors}
    # 临时日志是本函数生成的固定文件名，清理仅限工作区内这两个文件。
    Remove-Item -LiteralPath $stdout,$stderr -Force -ErrorAction SilentlyContinue
    return $result
}
function Eval-Page {
    <# 接收本地验收表达式，编码后在页面执行，返回结构化检查结果而不读取Cookie。 #>
    param([string]$Session,[string]$Expression)
    return (Browser $Session @('eval','-b',[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Expression)))) | ConvertFrom-Json
}
function Check {
    <# 接收标题和浏览器事实，保存逐项证据；不通过时立即停止，不冒充成功。 #>
    param([string]$Session,[string]$Name,[string]$Expression)
    $result=Eval-Page $Session $Expression
    $checks.Add([pscustomobject]@{name=$Name;result=$result})
    $checks | ConvertTo-Json -Depth 7 | Set-Content (Join-Path $output 'browser-checks.json') -Encoding utf8
    if($result.passed -ne $true){throw "浏览器验收失败：$Name"}
    Write-Host "通过：$Name"
}
# 两个独立浏览器检查管理员与成员，只读当前档案，不新增聊天或执行模型/工具。
$profiles=@(@('audit-conv-admin','admin','INITIAL_ADMIN_PASSWORD'),@('audit-conv-member','zhangsan','INITIAL_DEMO_PASSWORD'))
try {
    $lines=Get-Content (Join-Path $root '.env')
    foreach($profile in $profiles){
        $password=($lines | Where-Object {$_ -match ('^'+$profile[2]+'=')}) -replace ('^'+$profile[2]+'='),''
        $password | & npx --yes agent-browser auth save $profile[0] --url "$BaseUrl/login" --username $profile[1] --password-stdin --username-selector '#account' --password-selector '#password' --submit-selector '.primary-button' | Out-Null
        if($LASTEXITCODE -ne 0){throw '认证vault保存失败'}
        Browser $profile[0] @('open',"$BaseUrl/login") | Out-Null
        Browser $profile[0] @('wait','--fn','document.querySelector("#account")!==null||document.querySelector(".profile-button")!==null') | Out-Null
        $existing=Eval-Page $profile[0] '({logged_in:document.querySelector(".profile-button")!==null})'
        if(-not $existing.logged_in){Browser $profile[0] @('auth','login',$profile[0]) | Out-Null}
        Browser $profile[0] @('wait','--fn','document.querySelector(".profile-button")!==null') | Out-Null
    }
    Browser 'audit-conv-admin' @('set','viewport','1920','1080') | Out-Null
    $history=Eval-Page 'audit-conv-admin' '({items:Array.from(document.querySelectorAll(".history-item")).map(e=>e.textContent)})'
    $selected=Eval-Page 'audit-conv-admin' '(async()=>{const rows=await(await fetch("/api/admin/audit/conversations?limit=12")).json();let items=rows.items;for(let page=2;!items.some(c=>c.turns_count>=2&&c.username!=="admin")&&page<=6;page++){const v=await(await fetch("/api/admin/audit/conversations?limit=12&page="+page)).json();items=items.concat(v.items);}const c=items.find(c=>c.turns_count>=2&&c.username!=="admin")||items.find(c=>c.turns_count>=2);if(!c)throw new Error("没有可验收的多轮会话");const turns=await(await fetch("/api/admin/audit/conversations/"+c.conversation_id+"/turns")).json();const t=turns.items.find(t=>t.run_id&&t.assistant_message);if(!t)throw new Error("没有可验收的完成轮次");return {id:c.conversation_id,turns:c.turns_count,run:t.run_id,trace:t.trace_id,username:c.username};})()'
    Browser 'audit-conv-admin' @('find','role','button','click','--name','调试与审计','--exact') | Out-Null
    Browser 'audit-conv-admin' @('wait','--fn','document.querySelectorAll(".audit-session").length>0') | Out-Null
    Check 'audit-conv-admin' '最左侧个人历史保持原样' ('({passed:JSON.stringify(Array.from(document.querySelectorAll(".history-item")).map(e=>e.textContent))===JSON.stringify('+(ConvertTo-Json $history.items -Compress)+')})')
    Check 'audit-conv-admin' '会话列表随高度填满且卡片不拥挤' '(()=>{const rows=Array.from(document.querySelectorAll(".audit-session"));const box=document.querySelector(".audit-session-cards").getBoundingClientRect();const last=rows.at(-1).getBoundingClientRect();return {passed:rows.length>=5&&rows.length<=12&&rows.every(r=>r.getBoundingClientRect().height>=89)&&box.bottom-last.bottom<20,count:rows.length,minHeight:Math.min(...rows.map(r=>r.getBoundingClientRect().height)),gap:box.bottom-last.bottom};})()'
    $tall=Eval-Page 'audit-conv-admin' '({count:document.querySelectorAll(".audit-session").length})'
    Browser 'audit-conv-admin' @('set','viewport','1920','800') | Out-Null
    Browser 'audit-conv-admin' @('wait','--fn',('document.querySelectorAll(".audit-session").length<'+$tall.count)) | Out-Null
    Check 'audit-conv-admin' '短窗口自动减少每页会话数量' ('({passed:document.querySelectorAll(".audit-session").length<'+$tall.count+',count:document.querySelectorAll(".audit-session").length})')
    Browser 'audit-conv-admin' @('set','viewport','1920','1080') | Out-Null
    Browser 'audit-conv-admin' @('find','label','会话搜索','fill',$selected.id) | Out-Null
    Browser 'audit-conv-admin' @('find','role','button','click','--name','查询','--exact') | Out-Null
    Browser 'audit-conv-admin' @('wait','--fn','document.querySelectorAll(".audit-session").length===1') | Out-Null
    Browser 'audit-conv-admin' @('click','.audit-session') | Out-Null
    Browser 'audit-conv-admin' @('wait','--fn','document.querySelectorAll(".audit-turn-card").length>=2') | Out-Null
    Check 'audit-conv-admin' '完整会话逐轮显示用户与Agent内容' '({passed:Array.from(document.querySelectorAll(".audit-turn-card")).slice(0,2).every(e=>e.querySelector(".audit-chat-message.user")&&e.querySelector(".audit-chat-message.assistant"))})'
    Browser 'audit-conv-admin' @('click','.audit-turn-card header button') | Out-Null
    Browser 'audit-conv-admin' @('wait','--fn','document.querySelectorAll(".audit-event-toggle").length>0') | Out-Null
    Check 'audit-conv-admin' '中文本轮过程代替Operation树和关联JSON堆' '({passed:!document.querySelector(".audit-span")&&!document.querySelector(".audit-detail")&&Array.from(document.querySelectorAll(".audit-event-toggle strong")).some(e=>e.textContent.includes("主模型")||e.textContent.includes("Worker"))&&document.querySelector(".audit-usage-section").textContent.includes("总 Token")})'
    Browser 'audit-conv-admin' @('scrollintoview','.audit-event-toggle') | Out-Null
    Browser 'audit-conv-admin' @('click','.audit-event-toggle') | Out-Null
    Browser 'audit-conv-admin' @('wait','--fn','document.querySelector(".audit-event-row.open .audit-inline-json pre")!==null') | Out-Null
    Check 'audit-conv-admin' '点击后JSON直接位于对应事件下方' '(()=>{const row=document.querySelector(".audit-event-row.open");return {passed:row.querySelector(".audit-inline-json pre")!==null&&row.querySelector(".audit-event-toggle").getAttribute("aria-expanded")==="true"};})()'
    Browser 'audit-conv-admin' @('click','.audit-event-row.open .audit-event-toggle') | Out-Null
    Check 'audit-conv-admin' '再次点击原事件收起JSON' '({passed:!document.querySelector(".audit-inline-json")})'
    # 截图滚回所选会话顶部；右侧内滚动不带走左侧个人历史及会话分页。
    Eval-Page 'audit-conv-admin' '(()=>{document.querySelector(".audit-conversation-detail").scrollTop=0;return true;})()' | Out-Null
    Browser 'audit-conv-admin' @('screenshot',(Join-Path $output 'desktop.png')) | Out-Null
    Check 'audit-conv-admin' '桌面长内容不溢出且页脚不遮挡' '(()=>{const p=document.querySelector(".audit-page").getBoundingClientRect();const f=document.querySelector(".workspace-footer").getBoundingClientRect();return {passed:document.documentElement.scrollWidth<=innerWidth+2&&p.bottom<=f.top+1};})()'
    Browser 'audit-conv-admin' @('set','viewport','390','844') | Out-Null
    Check 'audit-conv-admin' '小屏无横向溢出' '({passed:document.documentElement.scrollWidth<=innerWidth+2,scroll:document.documentElement.scrollWidth,width:innerWidth})'
    Browser 'audit-conv-admin' @('screenshot',(Join-Path $output 'mobile.png')) | Out-Null
    Check 'audit-conv-member' '成员历史可见而全用户审计接口拒绝' '(async()=>{const r=await fetch("/api/admin/audit/conversations");return {passed:r.status===403&&document.querySelector(".history-section")!==null&&!Array.from(document.querySelectorAll(".nav-item")).some(e=>e.textContent.includes("调试与审计")),status:r.status};})()'
} finally {
    foreach($profile in $profiles){Browser $profile[0] @('close') | Out-Null}
}
