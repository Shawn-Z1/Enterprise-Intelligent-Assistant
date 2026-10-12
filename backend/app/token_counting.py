"""按主备型号使用官方本地参考分词器；云端聊天格式开销仍明确标为估算。"""
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class TokenCount:
    """保存计数、使用方法及分词器标识，不将本地计数声称为云端reported用量。"""
    count: int
    method: str
    tokenizer: str


def tokenizer_family(model: str) -> str:
    """接收实际模型名，返回支持的分词器家族；未知型号保留字节上界，不借用其他模型冒充。"""
    name=model.lower()
    if name in ('deepseek-flash','deepseek-v4.1-flash','deepseek-v4-flash','deepseek-v4-flash-vision-exp'):
        return 'deepseek_v41'
    if name.startswith('qwen'):
        return 'qwen'
    return 'unknown'


@lru_cache(maxsize=8)
def load_tokenizer(model: str):
    """接收主备模型名，校验并缓存官方分词器，返回对象/标识；已知型号加载失败不静默退回假计数。"""
    family=tokenizer_family(model)
    if family=='deepseek_v41':
        from tokenizers import Tokenizer
        root=Path(__file__).resolve().parents[1]/'tokenizer_assets'
        entry=json.loads((root/'manifest.json').read_text(encoding='utf-8'))['deepseek_v41']
        path=root/entry['filename']
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:
            raise RuntimeError('DeepSeek分词器未准备或哈希变化，请重新构建后端镜像')
        tokenizer=Tokenizer.from_file(str(path))
        tokenizer.no_padding();tokenizer.no_truncation()
        return tokenizer,'deepseek_v41_reference_'+entry['sha256'][:12]
    if family=='qwen':
        from dashscope import get_tokenizer
        return get_tokenizer(model),'dashscope_1.27.7_qwen_reference'
    return None,'unknown_model'


def count_text(text: str, model: str) -> TokenCount:
    """接收完整文本与型号，返回本地分词数量；不截断长文本，不将未知型号映射到不匹配词表。"""
    tokenizer,label=load_tokenizer(model)
    if tokenizer is None:
        return TokenCount(len(text.encode('utf-8')),'utf8_bytes_upper_bound_v1',label)
    if tokenizer_family(model)=='deepseek_v41':
        count=len(tokenizer.encode(text,add_special_tokens=False).ids)
    else:
        count=len(tokenizer.encode(text,allowed_special=set(),disallowed_special=()))
    return TokenCount(count,'local_reference_tokenizer_v1',label)


def count_value(value, model: str='') -> TokenCount:
    """接收文本或结构化消息/工具及型号，分词后附加聊天格式预留，返回预算计数与可追溯方法。"""
    text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,separators=(',',':'))
    counted=count_text(text,model)
    # 服务端Chat Completions隐藏模板不可完整复现，保留固定协议开销及槽位配置的安全余量。
    framing=64 + (8*len(value) if isinstance(value,list) else 0)
    method=counted.method+'_json_envelope' if counted.tokenizer!='unknown_model' else counted.method
    return TokenCount(counted.count+framing,method,counted.tokenizer)


def count_request(messages: list[dict], tools: list[dict] | None, model: str) -> TokenCount:
    """接收实际适配后的消息、工具schema和型号，返回完整输入预算，工具不因放在独立字段而漏算。"""
    messages_count=count_value(messages,model)
    tools_count=count_value(tools,model) if tools else None
    return TokenCount(messages_count.count+(tools_count.count if tools_count else 0),messages_count.method,messages_count.tokenizer)
