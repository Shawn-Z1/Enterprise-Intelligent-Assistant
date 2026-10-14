#!/bin/sh
# 从环境示例生成随机凭证；已有配置不覆盖，不要求宿主安装Python。
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
docker run --rm -i --user "$(id -u):$(id -g)" -v "$project_root:/workspace" -w /workspace python:3.12.12-slim-bookworm python - <<'PY'
from pathlib import Path
import secrets
p=Path('.env')
if not p.exists():
    text=Path('.env.example').read_text()
    while 'replace-with-random-password' in text:text=text.replace('replace-with-random-password',secrets.token_hex(32),1)
    p.write_text(text)
    p.chmod(0o600)
else:
    text=p.read_text()
    if not any(line.startswith('SANDBOX_CONTROL_TOKEN=') and line.split('=',1)[1].strip() for line in text.splitlines()):
        text+='\nSANDBOX_CONTROL_TOKEN='+secrets.token_hex(32)+'\n'
        if 'SANDBOX_ENABLED=' not in text:text+='\nSANDBOX_ENABLED=true\n'
    p.write_text(text)
print('环境配置已就绪，已有凭证保留，密钥不回显。')
PY
