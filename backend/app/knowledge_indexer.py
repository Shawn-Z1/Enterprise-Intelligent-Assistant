"""只为仍有效且已批准的文档建立可重建双路索引。"""
from app.ids import new_id
from app.rag_index import identifiers
from app.knowledge_chunking import split_content as split_document
from app.rag_contract import INDEX_VERSION


def split_content(parsed: dict, size: int, overlap: int) -> list[dict]:
    """接收真实解析、总字符上限及兼容的旧重叠参数，按层级/分隔符生成切片并提取精确编号，返回完整来源。"""
    pieces=split_document(parsed,size,overlap)
    for piece in pieces:
        piece['identifiers']=identifiers(piece['content'])
    return pieces


class KnowledgeIndexer:
    """PG 切片是可检查事实；索引成功且批准重新复核后才允许 worker 标记可检索。"""
    def __init__(self, pool, settings, index, audit):
        """接收连接池、切片配置、同空间双路索引与审计，保存依赖，不取得管理凭证。"""
        self.pool, self.settings, self.index, self.audit = pool, settings, index, audit

    async def build(self, row: dict, parsed: dict, job: dict, valid):
        """接收锁定任务的原件快照、不可变解析及批准校验函数，逐批真实向量化与索引，返回索引空间信息。"""
        info = await self.index.ensure()
        pieces = split_content(parsed, self.settings.rag_chunk_size, self.settings.rag_chunk_overlap)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current = dict(await conn.fetchrow('SELECT * FROM app_knowledge.applications WHERE application_id=$1 FOR UPDATE', row['application_id']))
                if not await valid(conn, current, job):
                    raise ValueError('批准已失效，禁止向量化')
                previous = await conn.fetchrow('SELECT embedding_space,index_version,collection_name FROM app_knowledge.documents WHERE document_id=$1', job['document_id'])
                if previous['embedding_space'] and previous['embedding_space']!=info['space'] and previous['index_version']>=INDEX_VERSION:
                    raise ValueError('不同 Embedding 空间必须独立重建，不能覆盖已有空间切片')
                await conn.execute("UPDATE app_knowledge.chunks SET status='superseded' WHERE document_id=$1 AND status='active' AND index_version<>$2",job['document_id'],INDEX_VERSION)
                for piece in pieces:
                    await conn.execute('''INSERT INTO app_knowledge.chunks(chunk_id,document_id,position,page_number,heading,content,acl_snapshot,
                        embedding_space,index_version,source_sha256,identifiers,heading_path,page_numbers,body_content,chunk_strategy,section_paths)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                        ON CONFLICT(document_id,position,index_version) DO NOTHING''', new_id(), job['document_id'], piece['position'],
                        piece['page_number'], piece['heading'], piece['content'], row['acl_snapshot'], info['space'], INDEX_VERSION,row['original_sha256'],piece['identifiers'],
                        piece['heading_path'],piece['page_numbers'],piece['body_content'],piece['chunk_strategy'],piece['section_paths'])
                await conn.execute("UPDATE app_knowledge.applications SET status='vectorizing',updated_at=now() WHERE application_id=$1", row['application_id'])
                await conn.execute("UPDATE app_knowledge.documents SET status='vectorizing',embedding_space=$1,index_version=$2,collection_name=$3 WHERE document_id=$4",info['space'],INDEX_VERSION,self.index.collection,job['document_id'])
                await conn.execute('UPDATE app_knowledge.jobs SET index_space=$1 WHERE job_id=$2', info['space'], job['job_id'])
        async with self.pool.acquire() as conn:
            chunks = [dict(value) for value in await conn.fetch('SELECT * FROM app_knowledge.chunks WHERE document_id=$1 AND embedding_space=$2 AND index_version=$3 ORDER BY position',job['document_id'],info['space'],INDEX_VERSION)]
        for offset in range(0, len(chunks), 16):
            async with self.pool.acquire() as conn:
                current = dict(await conn.fetchrow('SELECT * FROM app_knowledge.applications WHERE application_id=$1', row['application_id']))
                if not await valid(conn, current, job):
                    raise ValueError('批准或文档状态变化，停止索引')
            batch = chunks[offset:offset+16]
            embeddings = await self.index.embed([value['content'] for value in batch])
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    current = dict(await conn.fetchrow('SELECT * FROM app_knowledge.applications WHERE application_id=$1 FOR UPDATE', row['application_id']))
                    if not await valid(conn, current, job):
                        raise ValueError('批准在向量化期间失效，停止索引')
                    await conn.execute("UPDATE app_knowledge.applications SET status='indexing',updated_at=now() WHERE application_id=$1", row['application_id'])
                    await conn.execute("UPDATE app_knowledge.documents SET status='indexing' WHERE document_id=$1", job['document_id'])
                    await self.audit.append(job['trace_id'], 'knowledge.embedding_batch', {'application_id':row['application_id'], 'document_id':job['document_id'],
                        'space':info['space'], 'chunk_ids':[value['chunk_id'] for value in batch], 'model':embeddings['model'], 'revision':embeddings['revision'],
                        'input_tokens':embeddings['input_tokens'], 'duration_ms':embeddings['duration_ms']}, user_id=row['creator_user_id'], connection=conn)
            await self.index.upsert(batch, embeddings['values'])
        return {'space':info['space'],'collection':self.index.collection,'chunks':len(chunks),'index_version':INDEX_VERSION,
                'previous_collection':previous['collection_name'] if previous['collection_name']!=self.index.collection else None}

    async def remove(self, application_id: str):
        """接收申请编号，清理对应文档全部 Milvus 索引并失效 PG 派生切片，保留原件、审计与原聊天。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('SELECT document_id,collection_name FROM app_knowledge.documents WHERE application_id=$1', application_id)
        for row in rows:
            await self.index.delete_document(row['document_id'], row['collection_name'])
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE app_knowledge.chunks SET status='invalidated' WHERE document_id=$1", row['document_id'])
