"""自动个人记忆：PG版本为真源、Redis缓存小概要，详情仅经本人检索工具读取。"""
import hashlib
import json
import re
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.auth import auth_error
from app.audit import Usage
from app.ids import new_id
from app.model_gateway import estimate_tokens
from app.rag_service import live_context, rrf
from app.memory_index import MEMORY_INDEX_VERSION
from app.memory_consolidation import runtime_fields

MEMORY_PROCESSING_VERSION=3


class MemoryArguments(BaseModel):
    """查询只允许问题与目录主题编号，用户身份始终由后端登录态确定。"""
    model_config=ConfigDict(extra='forbid')
    query: str=Field(min_length=1,max_length=2000)
    topic_id: str | None=Field(default=None,min_length=1,max_length=16)


async def enqueue_memory(conn,run,message_id):
    """接收成功回复事务、可信run和助手水位，幂等加入后台提取；返回无值，提交后才可领取。"""
    await conn.execute("""INSERT INTO app_memory.jobs(job_id,user_id,conversation_id,job_type,status,source_watermark,run_id,trace_id,processing_version)
        VALUES($1,$2,$3,'extract','pending',$4,$5,$6,$7) ON CONFLICT(run_id,processing_version) WHERE job_type='extract' DO NOTHING""",
        new_id(),run['user_id'],run['conversation_id'],message_id,run['run_id'],run['trace_id'],MEMORY_PROCESSING_VERSION)


async def enqueue_index(conn,user_id,snapshot_id,trace_id,force=False):
    """接收本人有效快照及来源trace，在事务内幂等安排派生索引；失败旧作业可有限补建。"""
    await conn.execute("""INSERT INTO app_memory.jobs(job_id,user_id,job_type,status,snapshot_id,trace_id)
        VALUES($1,$2,'index','pending',$3,$4) ON CONFLICT(snapshot_id) WHERE job_type='index'
        DO UPDATE SET status='pending',attempts=0,available_at=now(),error_json=NULL
        WHERE ($5 AND app_memory.jobs.status='completed') OR
        (app_memory.jobs.status IN ('failed','cancelled') AND app_memory.jobs.updated_at<now()-interval '5 minutes')""",
        new_id(),user_id,snapshot_id,trace_id,force)


def directory_view(rows,settings,model):
    """接收本人主题元数据及预算，返回稳定名称/别名/简介目录；超量明确标记，正文不会混入。"""
    items=[]
    for row in rows:
        item={'topic_id':row['topic_key'],'title':row['title'],'aliases':row['aliases_json'],'summary':row['summary']}
        if estimate_tokens(items+[item],model)>settings.memory_directory_tokens:break
        items.append(item)
    return {'items':items,'total':len(rows),'truncated':len(items)<len(rows)}


class LongTermMemory:
    """可跨请求共享的无可变用户状态服务；概要不依赖Milvus在线，详细来源需当前有效快照。"""

    def __init__(self,pool,settings,index,audit):
        """接收PG、配置、私人索引和审计，创建有超时的Redis客户端；不触发检索或提取。"""
        self.pool,self.settings,self.index,self.audit=pool,settings,index,audit
        self.namespace='zhixing:memory:'+hashlib.sha256((settings.postgres_host+':'+settings.postgres_db).encode()).hexdigest()[:16]
        self.client=Redis(host=settings.redis_host,port=settings.redis_port,decode_responses=True,socket_timeout=0.5,socket_connect_timeout=0.5)

    async def close(self):
        """无参数释放Redis连接；索引生命周期由创建者管理，返回无值。"""
        await self.client.aclose()

    async def overview(self,user_id):
        """接收可信本人编号，先读PG有效版本再读穿缓存；返回完整有效偏好和有界目录，不召回正文。"""
        async with self.pool.acquire() as conn:
            state=await conn.fetchrow("SELECT s.* FROM app_memory.state s JOIN app_system.users u USING(user_id) WHERE s.user_id=$1 AND u.status='active'",user_id)
        if not state:return {'profile':{},'preferences':{},'topics':{'items':[],'total':0,'truncated':False},'snapshot_id':None}
        key=f"{self.namespace}:{user_id}:{state['snapshot_id']}"
        cached=None
        try:
            raw=await self.client.get(key)
            if raw and len(raw.encode())<128*1024:
                value=json.loads(raw)
                if value.get('user_id')==user_id and value.get('snapshot_id')==state['snapshot_id']:cached=value
        except (RedisError,ValueError,TypeError):pass
        if cached:return cached
        async with self.pool.acquire() as conn:
            snapshot=await conn.fetchrow('SELECT * FROM app_memory.snapshots WHERE snapshot_id=$1 AND user_id=$2',state['snapshot_id'],user_id)
            topics=await conn.fetch('SELECT topic_key,title,aliases_json,summary FROM app_memory.topics WHERE snapshot_id=$1 AND user_id=$2 ORDER BY title,topic_key',state['snapshot_id'],user_id)
        value={'user_id':user_id,'snapshot_id':state['snapshot_id'],'version':state['version'],
            'profile':{key:item['value'] for key,item in snapshot['profile_json'].items()},
            'profile_observed_at':{key:item.get('observed_at') for key,item in snapshot['profile_json'].items()},
            'preferences':{key:item['value'] for key,item in snapshot['preferences_json'].items()},
            'runtime_fields':runtime_fields(snapshot['profile_json'],snapshot['preferences_json']),
            'topics':directory_view(topics,self.settings,self.settings.primary_model)}
        try:await self.client.set(key,json.dumps(value,ensure_ascii=False),ex=self.settings.context_cache_ttl_seconds)
        except RedisError:pass
        return value

    async def valid_chunks(self,user_id,ids):
        """接收可信本人和候选切片编号，PG复核所有者/有效快照/空间，返回合法正文及来源。"""
        async with self.pool.acquire() as conn:
            rows=await conn.fetch("""SELECT c.*,t.topic_key,t.title,t.source_json,s.created_at
                FROM app_memory.chunks c JOIN app_memory.state st USING(user_id)
                JOIN app_memory.topics t ON t.topic_id=c.topic_id JOIN app_memory.snapshots s ON s.snapshot_id=c.snapshot_id
                JOIN app_system.users u ON u.user_id=c.user_id
                WHERE c.user_id=$1 AND c.chunk_id=ANY($2::varchar[]) AND c.snapshot_id=st.snapshot_id
                AND c.snapshot_id=st.indexed_snapshot_id AND t.user_id=c.user_id AND t.snapshot_id=c.snapshot_id
                AND c.embedding_space=st.embedding_space AND c.index_version=$3 AND u.status='active'""",user_id,ids,MEMORY_INDEX_VERSION)
        return [dict(row) for row in rows]

    async def validate(self,context,citations):
        """接收当前身份及本轮个人来源，复核所有切片仍有效；不把管理员作为跨用户读取例外。"""
        await live_context(self.pool,context.user_id,context.session_id)
        valid=await self.valid_chunks(context.user_id,[row['chunk_id'] for row in citations])
        return {row['chunk_id'] for row in valid}=={row['chunk_id'] for row in citations}

    async def search(self,args,context,trace_id,run_id):
        """接收严格工具参数和可信身份，真实执行私人混合检索；索引滞后返回明确状态并安排补建。"""
        started=perf_counter()
        context=await live_context(self.pool,context.user_id,context.session_id)
        async with self.pool.acquire() as conn:
            state=await conn.fetchrow('SELECT * FROM app_memory.state WHERE user_id=$1',context.user_id)
            topic=await conn.fetchval('SELECT topic_id FROM app_memory.topics WHERE user_id=$1 AND snapshot_id=$2 AND topic_key=$3',context.user_id,state['snapshot_id'] if state else None,args.topic_id) if args.topic_id else True
        output={'citations':[],'message':'本人尚无可用项目记忆','status':'empty'}
        if args.topic_id and not topic:
            output.update(message='主题不在本人当前目录中，请按名称重新检索或澄清',status='topic_not_found')
        elif state:
            try:
                await self.index.ensure()
                if state['indexed_snapshot_id']!=state['snapshot_id'] or state['embedding_space']!=self.index.info['space']:
                    async with self.pool.acquire() as conn:await enqueue_index(conn,context.user_id,state['snapshot_id'],trace_id,force=True)
                    output.update(message='个人概要可用，项目详细索引正在补建；暂不能据此断言没有历史记忆',status='index_pending')
                else:
                    vector=await self.index.embed([args.query],query=True)
                    await live_context(self.pool,context.user_id,context.session_id)
                    dense,bm25,expression=await self.index.recall_private(args.query,vector['values'][0],context.user_id,state['snapshot_id'],args.topic_id)
                    if not dense and not bm25:
                        async with self.pool.acquire() as conn:
                            count=await conn.fetchval('SELECT count(*) FROM app_memory.topics WHERE user_id=$1 AND snapshot_id=$2 AND ($3::varchar IS NULL OR topic_key=$3)',context.user_id,state['snapshot_id'],args.topic_id)
                            if count:
                                await enqueue_index(conn,context.user_id,state['snapshot_id'],trace_id,force=True)
                                output.update(message='项目索引缺失，后台已安排重建；暂不能断言没有记忆',status='index_pending')
                                await self.audit.append(trace_id,'memory.index_repair_queued',{'snapshot_id':state['snapshot_id']},user_id=context.user_id,run_id=run_id)
                                return output
                    fused=rrf(dense,bm25,self.settings.rag_rrf_k,self.settings.rag_rerank_limit)
                    valid=await self.valid_chunks(context.user_id,[row['chunk_id'] for row in fused])
                    by_id={row['chunk_id']:row for row in valid}
                    candidates=[by_id[row['chunk_id']] for row in fused if row['chunk_id'] in by_id]
                    scores=await self.index.rerank(args.query,candidates) if candidates else {'values':[]}
                    selected=sorted([(row,score) for row,score in zip(candidates,scores['values'],strict=True) if score>=self.settings.rag_min_score],key=lambda pair:-pair[1])[:self.settings.rag_final_limit]
                    valid_ids={row['chunk_id'] for row in await self.valid_chunks(context.user_id,[row['chunk_id'] for row,_ in selected])}
                    output={'status':'completed','citations':[{'type':'memory','label':str(i),'chunk_id':row['chunk_id'],
                        'topic_id':row['topic_key'],'snapshot_id':row['snapshot_id'],'title':row['title'],'content':row['content'],
                        'source_message_ids':row['source_json'].get('message_ids',[]),'remembered_at':row['created_at'].isoformat(),
                        'semantic_score':score} for i,(row,score) in enumerate(selected,1) if row['chunk_id'] in valid_ids],
                        'message':'已检索本人当前项目记忆；这是历史信息，不能代替当前企业事实'}
                    if not output['citations']:output['message']='本次检索未找到足够相关的本人项目记忆，不代表不存在其他历史主题'
                    await self.audit.append(trace_id,'memory.retrieval',{'query':args.model_dump(),'dense':dense,'bm25':bm25,'rrf':fused,
                        'rerank':[{'chunk_id':row['chunk_id'],'score':score} for row,score in zip(candidates,scores['values'],strict=True)],
                        'filter':expression,'output':output,'embedding':{key:value for key,value in vector.items() if key!='values'},
                        'reranker':{key:value for key,value in scores.items() if key!='values'},'duration_ms':(perf_counter()-started)*1000},user_id=context.user_id,run_id=run_id)
                    for kind,data in (('memory.query_embedding',vector),('memory.query_rerank',scores)):
                        if 'input_tokens' in data:
                            await self.audit.append(trace_id,kind,{key:value for key,value in data.items() if key!='values'},user_id=context.user_id,run_id=run_id,
                                usage=Usage(source='estimated',input_tokens=data['input_tokens'],model=data['model'],method='local_tokenizer_nonpadding_tokens_v1'))
            except Exception as error:
                # 会话失效不能伪装为普通索引故障，也不能继续返回已取得的私人正文。
                if getattr(error,'status_code',None) in (401,403):raise
                output={'citations':[],'error':{'code':'MEMORY_INDEX_UNAVAILABLE','message':'项目记忆检索暂不可用，个人概要仍可使用'},'message':'项目记忆检索暂不可用，不能断言未保存记忆','status':'unavailable'}
        await live_context(self.pool,context.user_id,context.session_id)
        await self.audit.append(trace_id,'memory.search_completed',{'query':args.model_dump(),'output':output},user_id=context.user_id,run_id=run_id)
        return output
