[CmdletBinding()]
param([string]$BaseUrl='http://127.0.0.1:8080',[string]$ResumeTag='')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$output=Join-Path $root 'verification/M05'
New-Item -ItemType Directory -Force -Path $output | Out-Null
$tag=if($ResumeTag){$ResumeTag}else{'M05-'+[Guid]::NewGuid().ToString('N').Substring(0,8)}
$member='zhixing-m05-member'; $admin='zhixing-m05-admin'; $other='zhixing-m05-other'
$checks=[Collections.Generic.List[object]]::new()
if($ResumeTag){foreach($item in (Get-Content (Join-Path $output 'browser-checks.json') -Raw | ConvertFrom-Json)){$checks.Add($item)}}

function Browser {
    <# 接收隔离会话及 CLI 参数，操作真实页面，不输出任何认证凭证。 #>
    param([string]$Session,[string[]]$Arguments)
    $value=& npx --yes agent-browser --session $Session @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw ($value -join "`n") }
    return $value -join "`n"
}

function Eval-Page {
    <# 接收会话和表达式，以编码形式安全发送 JavaScript，返回 JSON 结果。 #>
    param([string]$Session,[string]$Expression)
    return (Browser $Session @('eval','-b',[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Expression)))) | ConvertFrom-Json
}

function Check {
    <# 接收检查名及真实页面表达式，持久保存无凭证证据，失败立即中止。 #>
    param([string]$Session,[string]$Name,[string]$Expression)
    $value=Eval-Page $Session $Expression
    $checks.Add([pscustomobject]@{name=$Name;passed=$value.passed;evidence=$value})
    $checks | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $output 'browser-checks.json') -Encoding utf8
    if ($value.passed -ne $true) { throw "浏览器验收失败：$Name" }
    Write-Host "通过：$Name"
}

function Click-Visible {
    <# 接收会话、按钮名称，先滚动到真实元素再点击，适配长知识列表。 #>
    param([string]$Session,[string]$Name)
    $expression='(()=>{const name='+ (ConvertTo-Json $Name -Compress) +';const button=Array.from(document.querySelectorAll("button")).find(item=>item.textContent.trim()===name || item.getAttribute("aria-label")===name);if(button)button.scrollIntoView({block:"center"});})()'
    Eval-Page $Session $expression | Out-Null
    Browser $Session @('find','role','button','click','--name',$Name,'--exact') | Out-Null
}

$paths=@()
foreach ($kind in @('pdf','docx','txt','md','xlsx')) {
    $source=Join-Path $root "backend/tests/fixtures/m05/$(if($kind -eq 'xlsx'){'unsupported'}else{'policy'}).$kind"
    $target=Join-Path $output "$tag-policy.$kind"
    Copy-Item -LiteralPath $source -Destination $target
    $paths+=$target
}
$adminFile=Join-Path $output "$tag-admin.txt"
Copy-Item -LiteralPath (Join-Path $root 'backend/tests/fixtures/m05/policy.txt') -Destination $adminFile
$highFile=Join-Path $output "$tag-high.txt"
Copy-Item -LiteralPath (Join-Path $root 'backend/tests/fixtures/m05/policy.txt') -Destination $highFile
if(-not $ResumeTag){
$lines=Get-Content -LiteralPath (Join-Path $root '.env')
foreach ($profile in @(@($member,'zhangsan','INITIAL_DEMO_PASSWORD'),@($admin,'admin','INITIAL_ADMIN_PASSWORD'),@($other,'lisi','INITIAL_DEMO_PASSWORD'))) {
    $password=($lines | Where-Object { $_ -match ('^'+$profile[2]+'=') }) -replace ('^'+$profile[2]+'='),''
    $password | & npx --yes agent-browser auth save $profile[0] --url "$BaseUrl/login" --username $profile[1] --password-stdin --username-selector '#account' --password-selector '#password' --submit-selector '.primary-button'
    & npx --yes agent-browser --session $profile[0] open "$BaseUrl/login"
    Browser $profile[0] @('set','viewport','1440','1000') | Out-Null
    Browser $profile[0] @('auth','login',$profile[0]) | Out-Null
    Browser $profile[0] @('wait','--fn','document.querySelector(".profile-button")!==null') | Out-Null
    Browser $profile[0] @('find','role','button','click','--name','知识库管理','--exact') | Out-Null
    Browser $profile[0] @('wait','--fn','document.querySelector("#knowledge-grade")!==null') | Out-Null
}
Check $member '普通成员只有本人材料管理入口' '({passed:document.querySelector(".knowledge-tabs").textContent.includes("我的提交") && !document.querySelector(".knowledge-tabs").textContent.includes("全部材料")})'
Browser $member (@('upload','#knowledge-files')+$paths) | Out-Null
Browser $member @('check','input[type=checkbox][value=D2]') | Out-Null
Browser $member @('select','#knowledge-grade','3') | Out-Null
Click-Visible $member '提交审核'
Browser $member @('wait','--fn','document.querySelectorAll(".upload-results li").length===5') | Out-Null
Check $member '批量原件四项成功、非支持格式单项失败' '({passed:document.querySelectorAll(".upload-results li").length===5 && document.querySelectorAll(".upload-results li.error-text").length===1 && document.querySelector(".upload-results").textContent.includes("不支持")})'
$waitExpression='(async()=>{const rows=await(await fetch("/api/knowledge/applications")).json();return rows.filter(row=>row.filename.startsWith("'+$tag+'-policy")).length===4 && rows.filter(row=>row.filename.startsWith("'+$tag+'-policy")).every(row=>row.parse_status==="completed");})()'
Browser $member @('wait','--fn',$waitExpression) | Out-Null
Check $member '审核前只有预览，没有正式文档' ('(async()=>{const rows=await(await fetch("/api/knowledge/applications")).json();const own=rows.filter(row=>row.filename.startsWith("'+$tag+'-policy"));return {passed:own.length===4 && own.every(row=>row.status==="pending_review" && row.document_id===null)};})()')
}
Click-Visible $member "查看 $tag-policy.pdf"
Click-Visible $member '预览正文'
Browser $member @('wait','--fn','document.querySelector(".pdf-page-image")?.naturalWidth>0') | Out-Null
Check $member '真实中文 PDF 第一页生成图片' '({passed:document.querySelector(".pdf-page-image").naturalWidth>100 && document.querySelector(".pdf-toolbar").textContent.includes("1 / 2")})'
Click-Visible $member '放大 PDF'
Check $member 'PDF 放大后图片为 125%' '({passed:document.querySelector(".pdf-toolbar").textContent.includes("125%") && document.querySelector(".pdf-page-image").style.width==="125%"})'
Browser $member @('set','viewport','375','812') | Out-Null
Check $member 'PDF 缩放在小屏只在预览区滚动' '({passed:document.documentElement.scrollWidth<=innerWidth && getComputedStyle(document.querySelector(".pdf-image-scroll")).overflowX==="auto"})'
Browser $member @('set','viewport','1440','1000') | Out-Null
Browser $member @('find','role','button','click','--name','下一页','--exact') | Out-Null
Browser $member @('wait','--fn','document.querySelector(".pdf-toolbar").textContent.includes("2 / 2") && document.querySelector(".pdf-page-image")?.naturalWidth>0') | Out-Null
Check $member 'PDF 第二页单独鉴权预览' '({passed:document.querySelector(".pdf-toolbar").textContent.includes("2 / 2") && document.querySelector(".pdf-page-image").naturalWidth>100})'
Browser $member @('screenshot',(Join-Path $output 'pdf-preview.png')) | Out-Null
Click-Visible $member "查看 $tag-policy.docx"
Click-Visible $member '预览正文'
Browser $member @('wait','--fn','document.querySelector(".docx-table")!==null') | Out-Null
Check $member '真实 DOCX 段落和表格预览' '({passed:document.querySelector(".knowledge-preview").textContent.includes("通过前不可检索") && document.querySelectorAll(".docx-table td").length===4})'
Browser $member @('screenshot',(Join-Path $output 'docx-preview.png')) | Out-Null
Click-Visible $member "查看 $tag-policy.md"
Click-Visible $member '预览正文'
Browser $member @('wait','--fn','document.querySelector(".knowledge-preview")?.textContent.includes("危险链接")') | Out-Null
Check $member 'Markdown 展示文本且不执行脚本' '({passed:window.__m05Unsafe!==true && document.querySelector(".knowledge-preview").textContent.includes("<script>") && !document.querySelector(".knowledge-preview script")})'
$first=Eval-Page $member ('(async()=>{const rows=await(await fetch("/api/knowledge/applications")).json();return rows.find(row=>row.filename==="'+$tag+'-policy.pdf");})()')
Check $other '第二用户不能管理或读待审核原件' ('(async()=>{const response=await fetch("/api/knowledge/applications/'+$first.application_id+'/download");return {passed:response.status===404,status:response.status};})()')
Click-Visible $admin '待审核'
Browser $admin @('wait','--fn',('document.body.textContent.includes("'+$tag+'-policy.pdf")')) | Out-Null
Click-Visible $admin "查看 $tag-policy.pdf"
Check $admin '管理员审核不能修改申请 ACL' '({passed:document.querySelector(".knowledge-review").textContent.includes("不修改申请 ACL") && !document.querySelector(".knowledge-detail select")})'
Click-Visible $admin '通过'
Browser $admin @('wait','--fn','document.querySelector(".knowledge-detail").textContent.includes("待索引")') | Out-Null
Check $admin '实际批准与后台处理完成仍显示待索引' '({passed:document.querySelector(".knowledge-detail").textContent.includes("当前待索引") && !document.querySelector(".knowledge-detail").textContent.includes("可检索")})'
Browser $admin @('scrollintoview','#knowledge-files') | Out-Null
Browser $admin @('upload','#knowledge-files',$adminFile) | Out-Null
Browser $admin @('check','input[type=checkbox][value=D2]') | Out-Null
Browser $admin @('select','#knowledge-grade','3') | Out-Null
Click-Visible $admin '确认并导入'
Click-Visible $admin '我的提交'
Browser $admin @('wait','--fn',('(async()=>{const rows=await(await fetch("/api/knowledge/applications")).json();return rows.some(row=>row.filename==="'+$tag+'-admin.txt" && row.status==="pending_index");})()')) | Out-Null
Check $admin '管理员真实直接导入，无自我审核' ('(async()=>{const rows=await(await fetch("/api/knowledge/applications")).json();const row=rows.find(row=>row.filename==="'+$tag+'-admin.txt");const detail=await(await fetch("/api/knowledge/applications/"+row.application_id)).json();return {passed:row.status==="pending_index" && row.approval_origin==="admin_direct" && detail.reviews.length===0};})()')
Browser $member @('scrollintoview','#knowledge-files') | Out-Null
Browser $member @('upload','#knowledge-files',$highFile) | Out-Null
Browser $member @('select','#knowledge-grade','8') | Out-Null
Click-Visible $member '提交审核'
Browser $member @('wait','--fn',('document.querySelector(".knowledge-list").textContent.includes("'+$tag+'-high.txt")')) | Out-Null
Click-Visible $member "查看 $tag-high.txt"
Check $member '低职级创建者只有管理信息、预览下载禁用' '({passed:Array.from(document.querySelectorAll(".knowledge-actions button")).filter(button=>button.textContent.includes("预览正文") || button.textContent.includes("下载原件")).every(button=>button.disabled) && document.querySelector(".knowledge-detail").textContent.includes("管理信息仍可查看")})'
foreach ($size in @(@(1440,1000),@(768,1024),@(375,812))) {
    Browser $member @('set','viewport',[string]$size[0],[string]$size[1]) | Out-Null
    Check $member "$($size[0])px 知识页面无横向溢出" '({passed:document.documentElement.scrollWidth<=innerWidth,width:innerWidth,scrollWidth:document.documentElement.scrollWidth})'
}
Browser $member @('screenshot',(Join-Path $output 'knowledge-mobile.png')) | Out-Null
$cleanup='(async()=>{const csrf=decodeURIComponent(document.cookie.split("; ").find(row=>row.startsWith("zhixing_csrf=")).split("=")[1]);const rows=await(await fetch("/api/knowledge/applications?scope=all")).json();const own=rows.filter(row=>row.filename.startsWith("'+$tag+'"));for(const row of own){const response=await fetch("/api/knowledge/applications/"+row.application_id+"/delete",{method:"POST",headers:{"X-CSRF-Token":csrf}});if(!response.ok)throw Error("清理测试材料失败");}return {passed:own.length===6,count:own.length};})()'
Check $admin '仅通过产品接口失效本次六份测试材料，历史保留' $cleanup
foreach ($session in @($member,$admin,$other)) {
    Click-Visible $session '退出登录'
    Browser $session @('close') | Out-Null
    & npx --yes agent-browser auth delete $session | Out-Null
}
& npx --yes agent-browser close | Out-Null
$tag | Set-Content -LiteralPath (Join-Path $output 'browser-fixture-tag.txt') -Encoding utf8
