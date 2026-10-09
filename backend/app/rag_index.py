"""真实同空间向量与中文 BM25 索引接口；Milvus 仅保存可重建派生索引。"""
import asyncio
import hashlib
import json
import math
import re

import httpx
from pymilvus import DataType, Function, FunctionType, MilvusClient

from app.auth import auth_error
from app.ids import new_id
from app.rag_contract import INDEX_VERSION


def identifiers(text: str) -> list[str]:
    """接收正文或问题，抽取精确编号与日期，返回用于结构过滤的稳定小写值；不会伪造匹配。"""
    return sorted(set(value.lower() for value in re.findall(r'\b[A-Za-z]{2,}[A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+\b|\b\d{4}-\d{2}-\d{2}\b', text)))[:64]


class RagIndex:
    """封装小模型 HTTP 与 Milvus 同空间索引，失败明确抛错，不降级为关键词假重排。"""
    def __init__(self, settings):
        """接收配置，建立有界无自动重试客户端及初始化锁；不加载模型到 API 进程。"""
        self.settings = settings
        self.http = httpx.AsyncClient(base_url=settings.retrieval_model_url, timeout=httpx.Timeout(90, connect=5), trust_env=False)
        # PyMilvus 默认按 URI 复用全局连接；独立别名避免健康探针或另一实例关闭正在使用的连接。
        self.client = MilvusClient(uri=f'http://{settings.milvus_host}:{settings.milvus_port}', timeout=20, alias='rag-'+new_id())
        self.info, self.collection = None, None
        self.lock = asyncio.Lock()

    async def close(self):
        """无参数关闭本实例 HTTP 与 Milvus 连接，返回无值。"""
        await self.http.aclose()
        await asyncio.to_thread(self.client.close)

    async def model(self, endpoint: str, payload: dict | None=None) -> dict:
        """接收推理端点及可选输入，调用真实 CPU 服务；超时、空间或输出错误阻止继续检索。"""
        response = await self.http.get(endpoint) if payload is None else await self.http.post(endpoint, json=payload)
        if response.status_code==422:
            raise auth_error(422,'RETRIEVAL_INPUT_LIMIT',response.json().get('detail','检索模型输入超过限制，请缩短问题'))
        response.raise_for_status()
        value = response.json()
        if self.info and value.get('space') != self.info['space']:
            raise auth_error(409, 'EMBEDDING_SPACE_CHANGED', '检索模型空间已经变化，请重建该空间索引后再使用')
        return value

    async def ensure(self) -> dict:
        """无参数取得真实模型空间并初始化对应独立 collection；已有结构和版本不匹配即拒绝混用。"""
        async with self.lock:
            info = await self.model('/ready')
            if info['status']!='ready' or not all('CPUExecutionProvider' in values for values in info['providers'].values()):
                raise RuntimeError('检索模型尚未就绪')
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,50}', self.settings.rag_collection_prefix):
                raise ValueError('检索集合前缀格式不正确')
            collection = self.settings.rag_collection_prefix+'_'+hashlib.sha256(info['space'].encode()).hexdigest()[:12]
            description = json.dumps({'space':info['space'],'index_version':INDEX_VERSION,'analyzer':'jieba-lowercase-v1'},sort_keys=True)
            if not await asyncio.to_thread(self.client.has_collection, collection):
                schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False, description=description)
                schema.add_field('chunk_id', DataType.VARCHAR, is_primary=True, max_length=16)
                schema.add_field('document_id', DataType.VARCHAR, max_length=16)
                schema.add_field('space', DataType.VARCHAR, max_length=100)
                schema.add_field('index_version', DataType.INT64)
                schema.add_field('minimum_grade', DataType.INT64)
                schema.add_field('departments', DataType.ARRAY, element_type=DataType.VARCHAR, max_capacity=20, max_length=16)
                schema.add_field('identifiers', DataType.ARRAY, element_type=DataType.VARCHAR, max_capacity=64, max_length=64)
                schema.add_field('heading_path',DataType.JSON)
                schema.add_field('section_paths',DataType.JSON)
                schema.add_field('page_numbers',DataType.ARRAY,element_type=DataType.INT64,max_capacity=300)
                schema.add_field('text', DataType.VARCHAR, max_length=8192, enable_analyzer=True,
                                 analyzer_params={'tokenizer':'jieba', 'filter':['lowercase']})
                schema.add_field('dense', DataType.FLOAT_VECTOR, dim=info['dimension'])
                schema.add_field('sparse', DataType.SPARSE_FLOAT_VECTOR)
                schema.add_function(Function(name='chinese_bm25', function_type=FunctionType.BM25, input_field_names=['text'], output_field_names=['sparse']))
                indexes = self.client.prepare_index_params()
                indexes.add_index(field_name='dense', index_type='HNSW', metric_type='COSINE', params={'M':16,'efConstruction':100})
                indexes.add_index(field_name='sparse', index_type='SPARSE_INVERTED_INDEX', metric_type='BM25', params={'inverted_index_algo':'DAAT_MAXSCORE'})
                try:
                    await asyncio.to_thread(self.client.create_collection, collection_name=collection, schema=schema,
                                            index_params=indexes, consistency_level='Strong')
                except Exception:
                    if not await asyncio.to_thread(self.client.has_collection, collection):
                        raise
            actual = await asyncio.to_thread(self.client.describe_collection, collection)
            if actual.get('description') != description:
                raise auth_error(409, 'INDEX_SPACE_MISMATCH', '现有索引结构或模型空间不匹配，拒绝混用')
            self.info, self.collection = info, collection
            return info

    async def embed(self, texts: list[str], query=False) -> dict:
        """接收切片或查询文本，以同一固定模型生成真实单位向量，返回经过形状与有限值校验的响应。"""
        if not self.info:
            await self.ensure()
        value = await self.model('/embed', {'texts':texts, 'query':texts[0] if query else None})
        if len(value['values']) != len(texts) or any(len(row)!=self.info['dimension'] or not all(math.isfinite(number) for number in row) for row in value['values']):
            raise RuntimeError('检索模型向量维度或数值错误')
        return value

    async def upsert(self, chunks: list[dict], vectors: list[list[float]]):
        """接收已获批准的切片及同空间向量，原子覆盖相同切片键；返回无值，文本自动生成真实 BM25 稀疏向量。"""
        entries = []
        for row, vector in zip(chunks, vectors, strict=True):
            acl = row['acl_snapshot']
            entries.append({'chunk_id':row['chunk_id'], 'document_id':row['document_id'], 'space':row['embedding_space'],
                            'index_version':row['index_version'], 'minimum_grade':acl['minimum_grade'] or 0,
                            'departments':acl['allowed_department_ids'] or ['*'],'identifiers':row['identifiers'],'text':row['content'],'dense':vector,
                            'heading_path':row['heading_path'],'section_paths':row['section_paths'],'page_numbers':row['page_numbers']})
        await asyncio.to_thread(self.client.upsert, collection_name=self.collection, data=entries)
        await asyncio.to_thread(self.client.flush, self.collection)

    async def delete_document(self, document_id: str, collection: str | None=None):
        """接收文档与原集合名，只删除该文档派生向量；原件、原解析及聊天记录保持档案。"""
        name = collection or self.collection
        if name and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,80}', name) and await asyncio.to_thread(self.client.has_collection, name):
            await asyncio.to_thread(self.client.delete, collection_name=name, filter='document_id == '+json.dumps(document_id))
            await asyncio.to_thread(self.client.flush, name)

    async def recall(self, query: str, vector: list[float], document_ids: list[str], context) -> tuple[list[dict], list[dict], str]:
        """接收问题、向量及 PG 预授权文档集合，两路分别执行权限和编号过滤，返回真实排名与实际过滤表达式。"""
        filters = ['space == '+json.dumps(self.info['space']),'index_version == '+str(INDEX_VERSION),'document_id in '+json.dumps(document_ids)]
        if not context.is_admin:
            filters.append('minimum_grade <= '+str(context.grade))
            filters.append('(ARRAY_CONTAINS(departments, "*") or ARRAY_CONTAINS(departments, '+json.dumps(context.department_id)+'))')
        filters.extend('ARRAY_CONTAINS(identifiers, '+json.dumps(value)+')' for value in identifiers(query))
        expression = ' and '.join(filters)
        fields = ['document_id', 'space', 'index_version']
        dense, sparse = await asyncio.gather(
            asyncio.to_thread(self.client.search, collection_name=self.collection, data=[vector], anns_field='dense', filter=expression,
                              limit=self.settings.rag_recall_limit, output_fields=fields, search_params={'metric_type':'COSINE', 'params':{'ef':100}}),
            asyncio.to_thread(self.client.search, collection_name=self.collection, data=[query], anns_field='sparse', filter=expression,
                              limit=self.settings.rag_recall_limit, output_fields=fields, search_params={'metric_type':'BM25', 'params':{}}))
        convert = lambda results: [{'chunk_id':row.get('id',row.get('chunk_id')), 'score':float(row['distance']), 'rank':rank,
                                     'document_id':row['entity']['document_id']} for rank,row in enumerate(results[0],1)]
        return convert(dense), convert(sparse), expression

    async def rerank(self, query: str, chunks: list[dict]) -> dict:
        """接收问题及 PG 复核后的候选正文，调用独立交叉编码器，返回真实语义得分，不复用 RRF 充当重排。"""
        result = await self.model('/rerank', {'query':query, 'texts':[row['content'] for row in chunks]})
        if len(result['values'])!=len(chunks) or any(not math.isfinite(value) or not 0<=value<=1 for value in result['values']):
            raise RuntimeError('语义重排响应格式错误')
        return result
