"""构建时只获取固定哈希的分词器，不下载模型权重，运行时不联网加载。"""
import hashlib
import json
import urllib.request
from pathlib import Path


def prepare():
    """无参数读取同目录清单，复用校验通过的本地文件或下载，返回无模型权重的分词资产。"""
    root=Path(__file__).resolve().parent
    entry=json.loads((root/'manifest.json').read_text(encoding='utf-8'))['deepseek_v41']
    target=root/entry['filename']
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()==entry['sha256']:
        print('已校验并复用DeepSeek分词器。');return
    with urllib.request.urlopen(entry['url'],timeout=60) as response:
        data=response.read(20*1024*1024+1)
    if len(data)>20*1024*1024 or hashlib.sha256(data).hexdigest()!=entry['sha256']:
        raise RuntimeError('分词器大小或哈希不匹配，拒绝构建')
    target.write_bytes(data)
    print('已下载并校验DeepSeek分词器，不包含模型权重。')


if __name__=='__main__':prepare()
