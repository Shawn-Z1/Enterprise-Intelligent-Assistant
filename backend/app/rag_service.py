"""权限先于召回、观察与模型输入的完整混合 RAG 服务。"""
import json
from time import perf_counter

from app.audit import Usage
from app.auth import auth_error
from app.ids import new_id
from app.knowledge_service import acl_allows, fingerprint
from app.permissions import AuthContext
from app.rag_contract import INDEX_VERSION


async def live_context(pool, user_id: str, session_id: str) -> AuthContext:
    """接收服务端保存的用户和会话编号，从 PG 重读当前权限及会话有效性，返回可信上下文，失效直接拒绝。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''SELECT u.user_id,u.username,u.display_name,u.role,u.department_id,d.name AS department_name,
            u.grade,u.employee_id,u.position,u.knowledge_enabled,u.business_query_enabled,u.permission_version,s.session_id,s.csrf_token_hash
            FROM app_system.users u JOIN app_system.auth_sessions s USING(user_id) LEFT JOIN app_system.departments d USING(department_id)
            WHERE u.user_id=$1 AND s.session_id=$2 AND u.status='active' AND s.revoked_at IS NULL AND s.expires_at>now()''', user_id, session_id)
    if row is None:
        raise auth_error(401, 'UNAUTHENTICATED', '当前登录身份已失效，请重新登录')
    return AuthContext(**dict(row))


def rrf(dense: list[dict], sparse: list[dict], k: int, limit: int) -> list[dict]:
    """接收两路独立排名、平滑参数及候选上限，按 chunk_id 去重求倒数排名和，返回稳定融合顺序。"""
    fused = {}
    for branch, rows in (('dense', dense), ('bm25', sparse)):
        for row in rows:
            value = fused.setdefault(row['chunk_id'], {'chunk_id':row['chunk_id'], 'rrf_score':0.0})
            value['rrf_score'] += 1/(k+row['rank'])
            value[branch+'_rank'] = row['rank']
    return sorted(fused.values(), key=lambda row:(-row['rrf_score'], row['chunk_id']))[:limit]


class RagService:
    """企业知识检索服务，PG 当前文档/ACL 是唯一准入事实，历史证据只用于审计不作为检索缓存。"""
    def __init__(self, pool, settings, index, storage, audit):
        """接收 PG、限制、真实双路索引、原件与审计，保存依赖；不从模型参数接受权限。"""
        self.pool, self.settings, self.index, self.storage, self.audit = pool, settings, index, storage, audit

    async def allowed_documents(self, context) -> list[dict]:
        """接收最新身份，在召回前从 PG 取得合法可检索文档元数据，排除旧批准、旧模型空间和删除条目。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''SELECT a.*,d.document_id,d.embedding_space,d.index_version,d.approved_fingerprint,d.acl_snapshot AS document_acl
                FROM app_knowledge.documents d JOIN app_knowledge.applications a USING(application_id)
                WHERE d.status='retrievable' AND a.status='retrievable' AND d.embedding_space=$1 AND d.index_version=$2''',self.index.info['space'],INDEX_VERSION)
        return [dict(row) for row in rows if acl_allows(context,row) and fingerprint(dict(row))==row['approved_fingerprint'] and row['document_acl']==row['acl_snapshot']]

    async def validate(self, context, chunk_ids: list[str]) -> tuple[AuthContext, list[dict]]:
        """接收身份与候选键，再次读取当前 ACL/状态/版本及原件哈希；返回合法正文，绝不信任 Milvus 旧正文。"""
        context = await live_context(self.pool, context.user_id, context.session_id)
        if not context.can_use_knowledge:
            raise auth_error(403, 'KNOWLEDGE_NOT_AUTHORIZED', '尚未获得知识库权限，请联系管理员分配部门和职级')
        documents = {row['document_id']:row for row in await self.allowed_documents(context)}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('SELECT * FROM app_knowledge.chunks WHERE chunk_id=ANY($1::varchar[]) AND status=\'active\' AND embedding_space=$2 AND index_version=$3',chunk_ids,self.index.info['space'],INDEX_VERSION)
        result, verified = [], set()
        for value in rows:
            document = documents.get(value['document_id'])
            if not document or value['acl_snapshot']!=document['acl_snapshot'] or value['source_sha256']!=document['original_sha256']:
                continue
            if document['document_id'] not in verified:
                await self.storage.read_verified(document)
                verified.add(document['document_id'])
            result.append({**dict(value), 'filename':document['filename'], 'application_id':document['application_id']})
        context=await live_context(self.pool,context.user_id,context.session_id)
        if not context.can_use_knowledge:
            raise auth_error(403,'KNOWLEDGE_NOT_AUTHORIZED','知识权限已经变化，请联系管理员')
        latest={row['document_id'] for row in await self.allowed_documents(context)}
        return context, [row for row in result if row['document_id'] in latest]

    async def search(self, query: str, context, trace_id: str, run_id: str | None=None) -> dict:
        """接收问题及服务端身份，真实执行 dense→BM25→RRF→重排→PG 复核，保存四段证据并返回最多五个来源。"""
        started = perf_counter()
        if not query.strip() or len(query)>2000:
            raise auth_error(422, 'INVALID_QUERY', '检索问题不能为空或超过 2000 字符')
        context = await live_context(self.pool, context.user_id, context.session_id)
        if not context.can_use_knowledge:
            await self.audit.append(trace_id, 'rag.permission_denied', {'query':query}, user_id=context.user_id, run_id=run_id)
            raise auth_error(403, 'KNOWLEDGE_NOT_AUTHORIZED', '尚未获得知识库权限，请联系管理员分配部门和职级')
        await self.index.ensure()
        documents = await self.allowed_documents(context)
        dense, bm25, fused, ranked, expression = [], [], [], [], ''
        model_usage = []
        if documents:
            vector = await self.index.embed([query], query=True)
            model_usage.append(('rag.embedding', vector))
            # 向量生成可能耗时，召回前再次读取身份和准入文档，撤权不会先全库召回再隐藏。
            context=await live_context(self.pool,context.user_id,context.session_id)
            if not context.can_use_knowledge:
                raise auth_error(403,'KNOWLEDGE_NOT_AUTHORIZED','知识权限已经变化，请联系管理员')
            documents=await self.allowed_documents(context)
            if documents:
                dense, bm25, expression = await self.index.recall(query, vector['values'][0], [row['document_id'] for row in documents], context)
                fused = rrf(dense, bm25, self.settings.rag_rrf_k, self.settings.rag_rerank_limit)
                context, valid = await self.validate(context, [row['chunk_id'] for row in fused])
                by_id = {row['chunk_id']:row for row in valid}
                candidates = [by_id[row['chunk_id']] for row in fused if row['chunk_id'] in by_id]
                if candidates:
                    scores = await self.index.rerank(query, candidates)
                    model_usage.append(('rag.reranker', scores))
                    ranked = sorted([{'chunk_id':row['chunk_id'], 'semantic_score':score} for row,score in zip(candidates, scores['values'], strict=True)], key=lambda row:(-row['semantic_score'], row['chunk_id']))
        selected = [row for row in ranked if row['semantic_score']>=self.settings.rag_min_score][:self.settings.rag_final_limit]
        context, valid = await self.validate(context, [row['chunk_id'] for row in selected])
        by_id = {row['chunk_id']:row for row in valid}
        citations = []
        for rank, row in enumerate(selected, 1):
            chunk = by_id.get(row['chunk_id'])
            if chunk:
                citations.append({'label':str(rank), 'chunk_id':chunk['chunk_id'], 'document_id':chunk['document_id'], 'filename':chunk['filename'],
                    'page':chunk['page_number'], 'heading':chunk['heading'], 'position':chunk['position'], 'content':chunk['content'],
                    'page_numbers':chunk['page_numbers'],'heading_path':chunk['heading_path'],
                    'embedding_space':chunk['embedding_space'], 'index_version':chunk['index_version'], 'semantic_score':row['semantic_score']})
        retrieval_id = new_id()
        evidence = {'dense':dense, 'bm25':bm25, 'rrf':fused, 'rerank':ranked, 'citations':citations, 'filter':expression,
                    'space':self.index.info['space'], 'rrf_k':self.settings.rag_rrf_k, 'minimum_semantic_score':self.settings.rag_min_score,
                    'permission_version':context.permission_version, 'duration_ms':(perf_counter()-started)*1000}
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('''INSERT INTO app_runtime.retrievals(retrieval_id,trace_id,run_id,user_id,permission_version,query,evidence_json)
                    VALUES($1,$2,$3,$4,$5,$6,$7)''', retrieval_id, trace_id, run_id, context.user_id, context.permission_version, query, evidence)
                await self.audit.append(trace_id, 'rag.retrieval_completed', evidence, user_id=context.user_id, run_id=run_id,
                                        span_id=retrieval_id, span_name='rag.hybrid', duration_ms=evidence['duration_ms'], connection=conn)
                for kind, data in model_usage:
                    await self.audit.append(trace_id, kind, {key:value for key,value in data.items() if key!='values'}, user_id=context.user_id, run_id=run_id,
                        usage=Usage(source='estimated', input_tokens=data['input_tokens'], model=data['model'], method='local_tokenizer_nonpadding_tokens_v1'), connection=conn)
        return {'retrieval_id':retrieval_id, 'citations':citations, 'message':'找到当前授权的知识证据' if citations else '未找到足够相关且有权访问的企业证据，不能据此编造内部制度或数字。'}
