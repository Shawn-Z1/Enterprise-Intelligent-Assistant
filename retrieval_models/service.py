"""独立 CPU 检索推理服务：量化中文 Embedding 与中英交叉编码重排，不持有业务凭证。"""
import asyncio
import os
import resource
import time
from contextlib import asynccontextmanager

import numpy as np
import onnxruntime as ort
import psutil
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field
from tokenizers import Tokenizer

from download import LOCK, ROOT, verified

# 向量空间包含固定模型、量化文件、池化与归一化协议，不同模型不可查询同一空间。
SPACE = 'bge-small-zh-v15-int8-cls-norm-'+LOCK['embedding']['revision'][:8]+'-windowmean-v2'


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')
    texts: list[str] = Field(min_length=1, max_length=32)
    query: str | None = Field(default=None, max_length=2000)


class Engine:
    """加载两套固定模型，串行使用两个 CPU 线程并以小批次限制推理峰值。"""
    def __init__(self):
        """无参数校验权重、创建 CPU 会话及分词器；启动成功才提供就绪响应。"""
        verified()
        self.sessions, self.tokenizers = {}, {}
        self.lock = asyncio.Lock()
        self.calls = {'embedding': 0, 'reranker': 0}
        for name in LOCK:
            options = ort.SessionOptions()
            options.intra_op_num_threads = int(os.environ.get('MODEL_CPU_THREADS', '2'))
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            self.sessions[name] = ort.InferenceSession(str(ROOT/name/'onnx/model_quantized.onnx'), sess_options=options, providers=['CPUExecutionProvider'])
            tokenizer = Tokenizer.from_file(str(ROOT/name/'tokenizer.json'))
            tokenizer.enable_truncation(max_length=512,stride=64)
            # XLM-R 的 padding 编号为 1，中文 BERT 为 0；类型编号由实际编码返回。
            tokenizer.enable_padding(pad_id=1 if name=='reranker' else 0, pad_token='<pad>' if name=='reranker' else '[PAD]')
            self.tokenizers[name] = tokenizer

    def infer(self, name: str, texts: list[str], query: str | None) -> dict:
        """接收模型名、文本及可选问题，执行真实 ONNX CPU 推理，返回归一化向量或 sigmoid 重排分数与耗时。"""
        started, cpu = time.perf_counter(), time.process_time()
        output, tokens = [], 0
        tokenizer=self.tokenizers[name]
        tokenizer.no_padding();tokenizer.no_truncation()
        if name=='reranker' and len(tokenizer.encode(query,add_special_tokens=False).ids)>256:
            from fastapi import HTTPException
            raise HTTPException(422,'重排问题超过 256 tokens，请缩短问题，避免截断问题或正文')
        tokenizer.enable_truncation(max_length=512,stride=64,strategy='only_second' if name=='reranker' else 'longest_first')
        inputs=[(query,text) for text in texts] if name=='reranker' else [LOCK['embedding']['query_instruction']+text if query is not None else text for text in texts]
        groups=[tokenizer.encode(value[0],value[1]) if isinstance(value,tuple) else tokenizer.encode(value) for value in inputs]
        # 超过模型窗口时完整处理所有 overflow；每个逻辑切片仍只返回一个聚合向量或语义得分。
        frames,owners,window_counts=[],[],[]
        for owner,encoding in enumerate(groups):
            windows=[encoding]+encoding.overflowing
            if len(windows)>16:
                from fastapi import HTTPException
                raise HTTPException(422,'文本窗口数量超过限制，请缩短输入')
            window_counts.append(len(windows))
            frames.extend(windows);owners.extend([owner]*len(windows))
        values=[[] for _ in texts]
        for offset in range(0,len(frames),4):
            encodings=frames[offset:offset+4]
            length=max(len(value.ids) for value in encodings)
            pad=1 if name=='reranker' else 0
            tokens+=sum(sum(value.attention_mask) for value in encodings)
            fields={'input_ids':np.array([value.ids+[pad]*(length-len(value.ids)) for value in encodings],dtype=np.int64),
                    'attention_mask':np.array([value.attention_mask+[0]*(length-len(value.ids)) for value in encodings],dtype=np.int64),
                    'token_type_ids':np.array([value.type_ids+[0]*(length-len(value.ids)) for value in encodings],dtype=np.int64)}
            session = self.sessions[name]
            result = session.run(None, {item.name: fields[item.name] for item in session.get_inputs()})[0]
            if name=='embedding':
                vectors = result[:, 0, :] if result.ndim==3 else result
                vectors = vectors/np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
                for owner,vector in zip(owners[offset:offset+4],vectors,strict=True):
                    values[owner].append(vector)
            else:
                logits = result.reshape(-1)
                scores=1/(1+np.exp(-np.clip(logits,-60,60)))
                for owner,score in zip(owners[offset:offset+4],scores,strict=True):
                    values[owner].append(float(score))
        for rows in values:
            if name=='embedding':
                vector=np.mean(rows,axis=0)
                vector=vector/max(float(np.linalg.norm(vector)),1e-12)
                output.append(vector.astype(float).tolist())
            else:
                output.append(max(rows))
        self.calls[name] += 1
        return {'space': SPACE, 'dimension': LOCK['embedding']['dimension'], 'values': output, 'input_tokens': tokens,
                'duration_ms': (time.perf_counter()-started)*1000, 'cpu_seconds': time.process_time()-cpu, 'model': LOCK[name]['repo'],
                'revision': LOCK[name]['revision'], 'providers': self.sessions[name].get_providers(),
                'window_counts':window_counts,'truncated':False,'aggregation':'normalized_window_mean' if name=='embedding' else 'maximum_window_relevance'}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """接收服务实例，启动时加载真实模型，生命周期内保存引擎；退出时由进程释放原生模型。"""
    app.state.engine = Engine()
    yield


app = FastAPI(lifespan=lifespan)


@app.get('/ready')
async def ready():
    """无参数返回实际模型版本、CPU 执行器、权重体积与资源状态，不包含业务信息或凭证。"""
    engine = app.state.engine
    return {'status': 'ready', 'space': SPACE, 'dimension': LOCK['embedding']['dimension'],
            'models': LOCK, 'providers': {name: session.get_providers() for name, session in engine.sessions.items()},
            'weights_bytes': sum((ROOT/name/relative).stat().st_size for name, model in LOCK.items() for relative in model['files']),
            'resident_bytes': psutil.Process().memory_info().rss, 'peak_resident_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            'cpu_seconds': time.process_time(), 'calls': engine.calls}


@app.post('/embed')
async def embed(body: Input):
    """接收正文列表或查询文本，验证大小并串行推理；返回真实同空间向量，不提供固定测试向量。"""
    if any(not text.strip() or len(text)>4000 for text in body.texts):
        from fastapi import HTTPException
        raise HTTPException(422, '文本为空或超过限制')
    async with app.state.engine.lock:
        return await asyncio.to_thread(app.state.engine.infer, 'embedding', body.texts, body.query)


@app.post('/rerank')
async def rerank(body: Input):
    """接收问题及候选正文，使用真实交叉编码器评分；返回每个候选独立的语义分数。"""
    if not body.query or any(not text.strip() or len(text)>4000 for text in body.texts):
        from fastapi import HTTPException
        raise HTTPException(422, '问题或候选文本不正确')
    async with app.state.engine.lock:
        return await asyncio.to_thread(app.state.engine.infer, 'reranker', body.texts, body.query)
