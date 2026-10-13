"""私人记忆独立集合；推理、双路召回及重排复用企业RAG组件。"""
import asyncio
import hashlib
import json
import re

from pymilvus import DataType, Function, FunctionType
from app.rag_index import RagIndex

MEMORY_INDEX_VERSION = 1


class MemoryIndex(RagIndex):
    """仅替换集合结构和本人过滤，继承真实CPU向量、BM25双路和交叉编码器。"""

    async def ensure(self):
        """无参数初始化当前空间的私人集合；校验版本/结构，返回真实模型空间信息。"""
        async with self.lock:
            info=await self.model('/ready')
            if info['status']!='ready' or not all('CPUExecutionProvider' in row for row in info['providers'].values()):
                raise RuntimeError('记忆检索模型未就绪')
            prefix=self.settings.memory_collection_prefix
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,50}',prefix):raise ValueError('私人集合名称不合法')
            name=prefix+'_'+hashlib.sha256(info['space'].encode()).hexdigest()[:12]
            description=json.dumps({'space':info['space'],'memory_version':MEMORY_INDEX_VERSION,'analyzer':'jieba-lowercase-v1'},sort_keys=True)
            if not await asyncio.to_thread(self.client.has_collection,name):
                schema=self.client.create_schema(auto_id=False,enable_dynamic_field=False,description=description)
                for field in ('chunk_id','user_id','snapshot_id','topic_key'):
                    schema.add_field(field,DataType.VARCHAR,max_length=16,is_primary=field=='chunk_id')
                schema.add_field('space',DataType.VARCHAR,max_length=100)
                schema.add_field('index_version',DataType.INT64)
                schema.add_field('text',DataType.VARCHAR,max_length=8192,enable_analyzer=True,
                                 analyzer_params={'tokenizer':'jieba','filter':['lowercase']})
                schema.add_field('dense',DataType.FLOAT_VECTOR,dim=info['dimension'])
                schema.add_field('sparse',DataType.SPARSE_FLOAT_VECTOR)
                schema.add_function(Function(name='memory_bm25',function_type=FunctionType.BM25,input_field_names=['text'],output_field_names=['sparse']))
                indexes=self.client.prepare_index_params()
                indexes.add_index(field_name='dense',index_type='HNSW',metric_type='COSINE',params={'M':16,'efConstruction':100})
                indexes.add_index(field_name='sparse',index_type='SPARSE_INVERTED_INDEX',metric_type='BM25',params={'inverted_index_algo':'DAAT_MAXSCORE'})
                try:
                    await asyncio.to_thread(self.client.create_collection,collection_name=name,schema=schema,index_params=indexes,consistency_level='Strong')
                except Exception:
                    if not await asyncio.to_thread(self.client.has_collection,name):raise
            actual=await asyncio.to_thread(self.client.describe_collection,name)
            if actual.get('description')!=description:raise RuntimeError('私人记忆索引空间不匹配')
            self.info,self.collection=info,name
            return info

    async def store(self,rows,vectors):
        """接收PG切片及同空间向量，幂等写入私人集合；返回无值，不改变永久记忆。"""
        entries=[{'chunk_id':row['chunk_id'],'user_id':row['user_id'],'snapshot_id':row['snapshot_id'],
                  'topic_key':row['topic_key'],'space':row['embedding_space'],'index_version':MEMORY_INDEX_VERSION,
                  'text':row['content'],'dense':vector} for row,vector in zip(rows,vectors,strict=True)]
        if entries:
            await asyncio.to_thread(self.client.upsert,collection_name=self.collection,data=entries)
            await asyncio.to_thread(self.client.flush,self.collection)

    async def recall_private(self,query,vector,user_id,snapshot_id,topic_key=None):
        """接收查询及服务器用户/有效快照和可选主题；两路预过滤，返回候选排名及过滤证据。"""
        expression=' and '.join(['user_id == '+json.dumps(user_id),'snapshot_id == '+json.dumps(snapshot_id),
            'space == '+json.dumps(self.info['space']),'index_version == '+str(MEMORY_INDEX_VERSION)]
            +(['topic_key == '+json.dumps(topic_key)] if topic_key else []))
        dense,bm25=await self.search_branches(query,vector,expression,['snapshot_id','topic_key'])
        return dense,bm25,expression

    async def clean_previous(self,user_id,snapshot_id):
        """接收本人和新有效快照，删除本集合旧派生索引；不删除PG历史，失败可由补建重试。"""
        expression='user_id == '+json.dumps(user_id)+' and snapshot_id != '+json.dumps(snapshot_id)
        await asyncio.to_thread(self.client.delete,collection_name=self.collection,filter=expression)
