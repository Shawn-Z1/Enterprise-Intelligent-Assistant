[CmdletBinding()]
param([string]$BaseUrl='http://127.0.0.1:8080')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$output=Join-Path $root 'verification/M06'
New-Item -ItemType Directory -Force $output | Out-Null
$tag='M06-browser-'+[Guid]::NewGuid().ToString('N').Substring(0,8)
$filename=$tag+'.pdf'
$path=Join-Path $output $filename
Copy-Item -LiteralPath (Join-Path $root 'backend/tests/fixtures/m06/travel.pdf') -Destination $path
$member='zhixing-m06-member';$admin='zhixing-m06-admin';$other='zhixing-m06-other'
$checks=[Collections.Generic.List[object]]::new()

function Browser {
    <# 接收隔离会话和命令，操作真实页面并返回安全输出；认证凭证不进入参数或日志。 #>
    param([string]$Session,[string[]]$Arguments)
    $value=& npx --yes agent-browser --session $Session @Arguments 2>&1
    if($LASTEXITCODE -ne 0){throw ($value -join "`n")}
    return $value -join "`n"
}
function Eval-Page {
    <# 接收会话和 JavaScript，以编码形式传输并解析返回值，不打印 Cookie 或密钥。 #>
    param([string]$Session,[string]$Expression)
    return (Browser $Session @('eval','-b',[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Expression)))) | ConvertFrom-Json
}
function Check {
    <# 接收会话、检查标题及实际页面条件，保存逐项证据；任何失败立即停止，不能伪装通过。 #>
    param([string]$Session,[string]$Name,[string]$Expression)
    $value=Eval-Page $Session $Expression
    $checks.Add([pscustomobject]@{name=$Name;passed=$value.passed;evidence=$value})
    $checks | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $output 'browser-checks.json') -Encoding utf8
    if($value.passed -ne $true){throw "浏览器验收失败：$Name"}
    Write-Host "通过：$Name"
}
function Click-Visible {
    <# 接收会话和按钮名称，先滚动到对应元素再真实点击，兼容长目录与滚动面板。 #>
    param([string]$Session,[string]$Name)
    Eval-Page $Session ('(()=>{const name='+(ConvertTo-Json $Name -Compress)+';const button=Array.from(document.querySelectorAll("button")).find(item=>item.textContent.trim()===name || item.getAttribute("aria-label")===name);button?.scrollIntoView({block:"center"});})()') | Out-Null
    Browser $Session @('find','role','button','click','--name',$Name,'--exact') | Out-Null
}

$lines=Get-Content (Join-Path $root '.env')
foreach($profile in @(@($admin,'admin','INITIAL_ADMIN_PASSWORD'),@($member,'zhangsan','INITIAL_DEMO_PASSWORD'),@($other,'lisi','INITIAL_DEMO_PASSWORD'))){
    $password=($lines | Where-Object {$_ -match ('^'+$profile[2]+'=')}) -replace ('^'+$profile[2]+'='),''
    $password | & npx --yes agent-browser auth save $profile[0] --url "$BaseUrl/login" --username $profile[1] --password-stdin --username-selector '#account' --password-selector '#password' --submit-selector '.primary-button'
    & npx --yes agent-browser --session $profile[0] open "$BaseUrl/login"
    Browser $profile[0] @('set','viewport','1440','1000') | Out-Null
    Browser $profile[0] @('auth','login',$profile[0]) | Out-Null
    Browser $profile[0] @('wait','--fn','document.querySelector(".profile-button")!==null') | Out-Null
}
Click-Visible $admin '知识库管理'
Browser $admin @('wait','--fn','document.querySelector("#knowledge-files")!==null') | Out-Null
Browser $admin @('upload','#knowledge-files',$path) | Out-Null
Browser $admin @('check','input[type=checkbox][value=D2]') | Out-Null
Browser $admin @('select','#knowledge-grade','3') | Out-Null
Click-Visible $admin '确认并导入'
Browser $admin @('wait','--fn',('(async()=>{const rows=await(await fetch("/api/knowledge/applications")).json();return rows.some(row=>row.filename==="'+$filename+'" && row.status==="retrievable");})()')) | Out-Null
Check $admin '真实 PDF 经过后台解析与索引显示可检索' ('(async()=>{const rows=await(await fetch("/api/knowledge/applications")).json();const row=rows.find(row=>row.filename==="'+$filename+'");return {passed:row.status==="retrievable" && row.document_id!==null,application_id:row.application_id,document_id:row.document_id};})()')

Click-Visible $member '新建对话'
Browser $member @('fill','#message','根据我们公司的制度 ZX-M06-C66，国内差旅住宿每天最多报销多少钱？请给出来源。') | Out-Null
Click-Visible $member '发送消息'
Browser $member @('wait','--fn','document.querySelector(".message-sources button")!==null','--timeout','120000') | Out-Null
Check $member '普通成员实际 Agent 回答金额并显示来源引用' '({passed:Array.from(document.querySelectorAll(".chat-message.assistant")).some(row=>row.textContent.includes("650") && row.querySelector(".message-sources button"))})'
Check $member '消息卡片不显示模型型号或 token 计费' '({passed:!Array.from(document.querySelectorAll(".chat-message")).some(row=>/deepseek|qwen|tokens|token 用量/i.test(row.textContent))})'
$source=Eval-Page $member '(()=>{const button=document.querySelector(".message-sources button");return {label:button.getAttribute("aria-label")};})()'
Click-Visible $member $source.label
Browser $member @('wait','--fn','document.querySelector(".source-excerpt")?.textContent.includes("650")') | Out-Null
Check $member '右侧来源显示真实跨页范围并展示证据片段' '({passed:document.querySelector(".source-location").textContent.includes("第 1–2 页") && document.querySelector(".source-excerpt").textContent.includes("650")})'
Click-Visible $member '预览原件'
Browser $member @('wait','--fn','document.querySelector(".source-panel .pdf-page-image")?.naturalWidth>100') | Out-Null
Check $member '来源第一页真实 PDF 图片已加载' '({passed:document.querySelector(".source-panel .pdf-toolbar").textContent.includes("1 / 2") && document.querySelector(".source-panel .pdf-page-image").naturalWidth>100})'
Browser $member @('screenshot',(Join-Path $output 'source-desktop.png')) | Out-Null
Click-Visible $member '来源下一页'
Browser $member @('wait','--fn','document.querySelector(".source-panel .pdf-toolbar").textContent.includes("2 / 2") && document.querySelector(".pdf-page-image")?.naturalWidth>100') | Out-Null
Check $member '来源翻页重新读取第二页面' '({passed:document.querySelector(".source-panel .pdf-toolbar").textContent.includes("2 / 2")})'
Click-Visible $member '放大来源'
Check $member '来源预览支持 125% 缩放' '({passed:document.querySelector(".source-panel .pdf-toolbar").textContent.includes("125%")})'
foreach($size in @(@(1440,1000),@(768,1024),@(375,812))){
    Browser $member @('set','viewport',[string]$size[0],[string]$size[1]) | Out-Null
    Check $member "$($size[0])px 来源与对话无页面横向溢出" '({passed:document.documentElement.scrollWidth<=innerWidth,width:innerWidth,scrollWidth:document.documentElement.scrollWidth})'
}
Browser $member @('screenshot',(Join-Path $output 'source-mobile.png')) | Out-Null
Browser $member @('press','Escape') | Out-Null
Check $member 'Escape 关闭来源并恢复引用按钮焦点' '({passed:!document.querySelector(".source-panel") && document.activeElement.closest(".message-sources")!==null})'
Browser $member @('set','viewport','1440','1000') | Out-Null
Click-Visible $member $source.label
Browser $member @('wait','--fn','document.querySelector(".source-excerpt")?.textContent.includes("650")') | Out-Null
$citation=Eval-Page $member '(async()=>{const conversations=await(await fetch("/api/conversations")).json();const detail=await(await fetch("/api/conversations/"+conversations[0].conversation_id)).json();return {conversation_id:detail.conversation_id,source:detail.messages.find(row=>row.citations?.length).citations[0]};})()'
Check $other '其他部门不能打开来源或预览原件' ('(async()=>{const source=await fetch("/api/knowledge/sources/'+$citation.source.chunk_id+'");const preview=await fetch("/api/knowledge/documents/'+$citation.source.document_id+'/preview");return {passed:source.status===403 && preview.status===403,source_status:source.status,preview_status:preview.status};})()')
$cleanup='(async()=>{const csrf=decodeURIComponent(document.cookie.split("; ").find(row=>row.startsWith("zhixing_csrf=")).split("=")[1]);const rows=await(await fetch("/api/knowledge/applications")).json();const row=rows.find(row=>row.filename==="'+$filename+'");const response=await fetch("/api/knowledge/applications/"+row.application_id+"/delete",{method:"POST",headers:{"X-CSRF-Token":csrf}});return {passed:response.ok,application_id:row.application_id};})()'
Check $admin '通过产品接口删除本次知识并请求索引清理' $cleanup
Browser $member @('wait','--fn','document.querySelector(".source-panel .error-text")!==null && !document.querySelector(".source-excerpt")') | Out-Null
Check $member '已打开来源在知识删除后清空正文和图片' '({passed:!document.querySelector(".source-excerpt") && !document.querySelector(".source-panel .pdf-page-image") && document.querySelector(".source-panel .error-text")!==null})'
Check $member '知识删除独立于原聊天，历史回答完整保留' ('(async()=>{const detail=await(await fetch("/api/conversations/'+$citation.conversation_id+'")).json();return {passed:detail.messages.some(row=>row.role==="assistant" && row.content.includes("650")),citations_without_replayed_body:detail.messages.filter(row=>row.citations?.length).every(row=>row.citations.every(source=>source.content===undefined))};})()')
Click-Visible $member '关闭知识来源'
Click-Visible $member '新建对话'
Browser $member @('fill','#message','计算 13 加 29，用一句话回答。') | Out-Null
Click-Visible $member '发送消息'
Browser $member @('wait','--fn','Array.from(document.querySelectorAll(".chat-message.assistant")).some(row=>row.textContent.includes("42"))','--timeout','120000') | Out-Null
Check $member '普通问题直接回答且没有来源卡片' '({passed:document.querySelector(".chat-message.assistant").textContent.includes("42") && !document.querySelector(".message-sources")})'
Browser $admin @('wait','--fn',('(async()=>{const rows=await(await fetch("/api/knowledge/applications")).json();return rows.some(row=>row.filename==="'+$filename+'" && row.status==="invalidated");})()')) | Out-Null
foreach($session in @($member,$admin,$other)){
    Click-Visible $session '退出登录'
    Browser $session @('close') | Out-Null
    & npx --yes agent-browser auth delete $session | Out-Null
}
& npx --yes agent-browser close | Out-Null
[pscustomobject]@{tag=$tag;filename=$filename;conversation_id=$citation.conversation_id;chunk_id=$citation.source.chunk_id;document_id=$citation.source.document_id} | ConvertTo-Json | Set-Content (Join-Path $output 'browser-fixture.json') -Encoding utf8
