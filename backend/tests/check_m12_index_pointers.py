"""恢复后派生索引指针的定向核对；所有状态的文档都不能再指向源Milvus集合。"""
import asyncio
import json
from pathlib import Path
from app.config import Settings
from app.db_admin import BootstrapSettings,connect_admin
from app.maintenance import rebuild


async def main():
    """读取实际恢复配置，对指定新库幂等补建并检查指针；不访问源集合、不启动后台/云模型。"""
    root=Path('/backup');environment=json.loads((root/'restore.compose.json').read_text())['services']['api']['environment']
    database=environment['POSTGRES_DB'];base=BootstrapSettings()
    if database==base.postgres_db or not database.startswith('m12_restore_'):raise ValueError('仅允许明确的隔离恢复库')
    conn=await connect_admin(base.model_copy(update={'postgres_db':database}))
    settings=Settings().model_copy(update={'rag_collection_prefix':environment['RAG_COLLECTION_PREFIX'],'memory_collection_prefix':environment['MEMORY_COLLECTION_PREFIX']})
    try:
        result=await rebuild(conn,settings)
        wrong=await conn.fetchval('SELECT count(*) FROM app_knowledge.documents WHERE collection_name IS NOT NULL AND collection_name<>$1',result['knowledge_collection'])
        inactive=await conn.fetchval("SELECT count(*) FROM app_knowledge.documents WHERE status<>'retrievable' AND collection_name=$1",result['knowledge_collection'])
        assert wrong==0,'仍存在指向源集合的文档'
        proof={'passed':True,'all_document_pointers_isolated':True,'inactive_documents_checked':inactive,'indexes':result}
        (root/'index-pointer-verification.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2))
        print(json.dumps(proof,ensure_ascii=False))
    finally:await conn.close()


if __name__=='__main__':asyncio.run(main())
