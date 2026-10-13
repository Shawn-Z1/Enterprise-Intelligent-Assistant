[CmdletBinding()]
param([string]$BaseUrl='http://127.0.0.1:8080')
# M12审计界面已改为完整会话视图，保留原命令入口并转到新的只读浏览器验收。
$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'verify-browser-audit-conversations.ps1') -BaseUrl $BaseUrl
