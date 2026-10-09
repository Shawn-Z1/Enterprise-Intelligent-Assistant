"""下载固定 ONNX 模型并验证每个权重与分词器，不执行远程模型代码。"""
import hashlib
import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get('MODEL_CACHE', '/models'))
LOCK = json.loads(Path(__file__).with_name('model_lock.json').read_text(encoding='utf-8'))


def digest(path: Path) -> str:
    """接收本地文件路径，流式计算 SHA-256 并返回摘要，避免把大型权重全部复制到内存。"""
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024*1024), b''):
            value.update(block)
    return value.hexdigest()


def verified() -> None:
    """无参数检查固定模型全部文件的实际哈希；缺失或被修改即阻止推理，返回无值。"""
    for name, model in LOCK.items():
        for relative, expected in model['files'].items():
            path = ROOT/name/relative
            if not path.is_file() or digest(path) != expected:
                raise RuntimeError('模型缓存缺失或校验失败：'+name+'/'+relative)


def download() -> None:
    """无参数下载锁定提交的文件到持久缓存，校验后原子替换；已有正确文件直接复用。"""
    endpoint = os.environ.get('MODEL_DOWNLOAD_ENDPOINT', 'https://huggingface.co').rstrip('/')
    if not endpoint.startswith('https://'):
        raise ValueError('模型下载端点必须使用 HTTPS')
    for name, model in LOCK.items():
        for relative, expected in model['files'].items():
            path = ROOT/name/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file() and digest(path) == expected:
                continue
            temporary = path.with_suffix(path.suffix+'.partial')
            url = endpoint+'/'+model['repo']+'/resolve/'+model['revision']+'/'+relative
            request = urllib.request.Request(url, headers={'User-Agent': 'zhixing-model-cache/0.6'})
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open('wb') as output:
                while block := response.read(1024*1024):
                    output.write(block)
            if digest(temporary) != expected:
                temporary.unlink()
                raise RuntimeError('下载文件哈希不符，拒绝使用：'+name+'/'+relative)
            temporary.replace(path)
            print('已验证模型缓存：'+name+'/'+relative, flush=True)
    verified()


if __name__ == '__main__':
    download()
