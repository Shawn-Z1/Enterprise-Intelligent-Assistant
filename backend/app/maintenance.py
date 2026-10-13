"""维护窗口备份与新目标恢复：PG和应用原件为事实源，缓存及向量索引可重建。"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from minio import Minio, MinioAdmin
from minio.credentials import StaticProvider
from app.audit import AuditRecorder
from app.config import Settings
from app.db_admin import BootstrapSettings, connect_admin, identifier, provision_roles, grant_runtime_access
from app.ids import new_id
from app.rag_index import RagIndex
from app.memory_index import MemoryIndex


def digest(path):
    """接收备份文件路径，以流式读取计算SHA256，返回十六进制摘要。"""
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def write_json(path,value):
    """接收输出路径和可序列化内容，独占创建新文件并收紧权限；已有文件不覆盖。"""
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2,default=str)


def minio_clients():
    """无参数从维护容器环境读取root凭证，返回数据与管理SDK；不向API/worker传递root。"""
    endpoint=os.environ.get('APP_MINIO_ENDPOINT','minio:9000')
    key,password=os.environ['MINIO_ROOT_USER'],os.environ['MINIO_ROOT_PASSWORD']
    return Minio(endpoint,access_key=key,secret_key=password,secure=False),MinioAdmin(endpoint=endpoint,credentials=StaticProvider(key,password),secure=False)


def pg_command(settings,program,arguments):
    """接收管理配置、备份程序和参数，用进程环境提供密码；失败抛错且不打印连接凭证。"""
    environment=os.environ.copy()
    environment.update(PGHOST=settings.postgres_host,PGPORT=str(settings.postgres_port),PGUSER=settings.postgres_user,
                       PGPASSWORD=settings.postgres_password.get_secret_value())
    result=subprocess.run([program,*arguments],env=environment,capture_output=True,text=True)
    if result.returncode:raise RuntimeError(f'{program}失败：'+result.stderr[-2000:])


async def counts(conn):
    """接收管理连接，统计应用及演示业务的基础表行数；返回用于原始恢复一致性核对的清单。"""
    tables=await conn.fetch("SELECT schemaname,tablename FROM pg_tables WHERE schemaname LIKE 'app_%' OR schemaname='demo_business' ORDER BY 1,2")
    return {f'{row["schemaname"]}.{row["tablename"]}':await conn.fetchval(f'SELECT count(*) FROM "{row["schemaname"]}"."{row["tablename"]}"') for row in tables}


async def backup(root,settings):
    """接收空备份目录与源配置，在已停写维护窗口导出PG和全部应用对象，并生成SHA/行数清单。"""
    os.umask(0o077)
    root=Path(root);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if any(root.iterdir()):raise ValueError('备份目录必须为空，禁止覆盖已有备份')
    # 对象文件名仅由key哈希生成，源文件名称不能成为本地路径。
    objects=root/'objects';objects.mkdir(mode=0o700)
    client,_=minio_clients();bucket=os.environ.get('APP_MINIO_BUCKET','application-files')
    conn=await connect_admin(settings)
    try:
        manifest={'format':1,'created_at':datetime.now(timezone.utc).isoformat(),'source_database':settings.postgres_db,
                  'source_bucket':bucket,'counts':await counts(conn),'objects':[],'consistency':'maintenance-window'}
        await asyncio.to_thread(pg_command,settings,'pg_dump',['--format=custom','--file='+str(root/'database.dump'),settings.postgres_db])
        for item in client.list_objects(bucket,recursive=True):
            name=hashlib.sha256(item.object_name.encode()).hexdigest()
            path=objects/name;client.fget_object(bucket,item.object_name,str(path))
            stat=client.stat_object(bucket,item.object_name)
            manifest['objects'].append({'key':item.object_name,'file':name,'size':path.stat().st_size,'sha256':digest(path),'content_type':stat.content_type})
        manifest['database_sha256']=digest(root/'database.dump')
        write_json(root/'manifest.json',manifest)
        print(json.dumps({'backup':'completed','tables':len(manifest['counts']),'objects':len(manifest['objects'])}))
        return manifest
    finally:await conn.close()


def validate(root):
    """接收备份目录，检查格式/PG及每个对象SHA和安全本地路径；返回有效清单，不执行档案内容。"""
    root=Path(root);manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format')!=1 or digest(root/'database.dump')!=manifest['database_sha256']:raise ValueError('备份PG指纹不匹配')
    seen=set()
    for row in manifest['objects']:
        if not re.fullmatch('[0-9a-f]{64}',row['file']) or row['file']!=hashlib.sha256(row['key'].encode()).hexdigest() or row['key'] in seen:raise ValueError('对象清单路径或键不合法')
        seen.add(row['key']);path=root/'objects'/row['file']
        if path.is_symlink() or path.stat().st_size!=row['size'] or digest(path)!=row['sha256']:raise ValueError('备份对象指纹不匹配')
    return manifest


def scoped_identity(client,admin,bucket):
    """接收维护SDK和新Bucket，创建独立应用身份/最小权限策略；返回新凭证，不扩大原应用权限。"""
    key='restore-'+secrets.token_hex(10);password=secrets.token_urlsafe(32)
    admin.user_add(key,password)
    admin.policy_add(key,policy={'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Action':['s3:ListBucket','s3:GetBucketLocation'],'Resource':['arn:aws:s3:::'+bucket]},
        {'Effect':'Allow','Action':['s3:GetObject','s3:PutObject','s3:DeleteObject','s3:AbortMultipartUpload','s3:ListMultipartUploadParts'],'Resource':['arn:aws:s3:::'+bucket+'/*']}]})
    admin.policy_set(key,user=key)
    return key,password


async def rebuild(conn,settings):
    """接收恢复库连接/独立集合配置，用固定真实CPU模型重建有效知识及私人切片；不改PG历史正文。"""
    knowledge=RagIndex(settings);memory=MemoryIndex(settings)
    try:
        info=await knowledge.ensure();await memory.ensure()
        # 删除中/已失效文档也必须改派生指针，防止恢复后清理作业访问共享Milvus上的源集合。
        await conn.execute('UPDATE app_knowledge.documents SET collection_name=$1 WHERE collection_name IS NOT NULL',knowledge.collection)
        documents=await conn.fetch("SELECT document_id,embedding_space FROM app_knowledge.documents WHERE status='retrievable'")
        total=0
        for document in documents:
            if document['embedding_space']!=info['space']:raise RuntimeError('恢复所需检索模型空间不同，请安装备份时固定的模型版本')
            rows=await conn.fetch("SELECT * FROM app_knowledge.chunks WHERE document_id=$1 AND status='active' AND embedding_space=$2 ORDER BY position",document['document_id'],info['space'])
            for offset in range(0,len(rows),16):
                batch=[dict(row) for row in rows[offset:offset+16]]
                vectors=await knowledge.embed([row['content'] for row in batch]);await knowledge.upsert(batch,vectors['values'])
            await conn.execute('UPDATE app_knowledge.documents SET collection_name=$2 WHERE document_id=$1',document['document_id'],knowledge.collection)
            total+=len(rows)
        states=await conn.fetch('SELECT user_id,snapshot_id FROM app_memory.state')
        personal=0
        for state in states:
            rows=await conn.fetch('''SELECT c.*,t.topic_key FROM app_memory.chunks c JOIN app_memory.topics t USING(topic_id)
                WHERE c.user_id=$1 AND c.snapshot_id=$2 ORDER BY c.position''',state['user_id'],state['snapshot_id'])
            if any(row['embedding_space']!=info['space'] for row in rows):raise RuntimeError('私人记忆模型空间不同，拒绝混用')
            for offset in range(0,len(rows),16):
                batch=[dict(row) for row in rows[offset:offset+16]]
                vectors=await memory.embed([row['content'] for row in batch]);await memory.store(batch,vectors['values'])
            await conn.execute('UPDATE app_memory.state SET indexed_snapshot_id=$2,embedding_space=$3 WHERE user_id=$1',state['user_id'],state['snapshot_id'],info['space'])
            personal+=len(rows)
        return {'knowledge_chunks':total,'personal_chunks':personal,'knowledge_collection':knowledge.collection,'personal_collection':memory.collection}
    finally:await knowledge.close();await memory.close()


async def quarantine(conn,source_bucket,target_bucket):
    """接收新恢复库及Bucket映射，使旧登录/确认/运行失效；返回中断任务数，保留全部历史和文件。"""
    async with conn.transaction():
        await conn.execute('UPDATE app_runtime.attachments SET bucket=$2 WHERE bucket=$1',source_bucket,target_bucket)
        await conn.execute('UPDATE app_system.auth_sessions SET revoked_at=COALESCE(revoked_at,now())')
        changed=await conn.fetch("""UPDATE app_runtime.runs SET status='interrupted',lease_owner=NULL,lease_until=NULL,updated_at=now(),
            state_json=state_json || '{"restore_interrupted":true}'::jsonb WHERE status NOT IN ('completed','failed','interrupted','cancelled') RETURNING run_id""")
        await conn.execute("UPDATE app_runtime.query_approvals SET decision='invalidated' WHERE decision IN ('pending','approved','executing')")
        await conn.execute("UPDATE app_runtime.sandbox_jobs SET status='unknown',cancel_requested=true,updated_at=now() WHERE status IN ('queued','running')")
        # 新恢复配置使用新的执行存储卷，不携带旧guest；解除新库旧额度，不宣称原宿主容器已清理。
        await conn.execute("UPDATE app_runtime.sandbox_jobs SET resources_released=true WHERE NOT resources_released")
        await conn.execute("UPDATE app_runtime.sandbox_nodes SET draining=true,expires_at=now()")
        await conn.execute("UPDATE app_memory.jobs SET status='pending',lease_owner=NULL,lease_until=NULL WHERE status='running'")
        await conn.execute("UPDATE app_knowledge.jobs SET status='pending',lease_owner=NULL,lease_until=NULL WHERE status='running'")
    return len(changed)


async def restore(root,settings,database,bucket):
    """接收有效备份和全新DB/Bucket名称，恢复事实源、隔离未知任务、重建索引并输出受限切换配置。"""
    os.umask(0o077)
    root=Path(root);manifest=validate(root);identifier(database)
    if len(database)>38 or database in (settings.postgres_db,manifest['source_database']) or not re.fullmatch('[a-z][a-z0-9-]{2,60}',bucket) or bucket in (manifest['source_bucket'],'milvus-data'):raise ValueError('恢复必须指定不同的新库（最多38字符）和新应用Bucket')
    if (root/'restore.compose.json').exists():raise ValueError('此备份已有恢复配置，请使用副本或新备份目录')
    client,admin=minio_clients();control=await connect_admin(settings)
    target_settings=settings.model_copy(update={'postgres_db':database})
    conn=None
    try:
        if await control.fetchval('SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)',database) or client.bucket_exists(bucket):raise ValueError('恢复目标已存在，禁止覆盖')
        owner=settings.app_db_user+'_owner'
        await provision_roles(control,settings)
        await control.execute(f'CREATE DATABASE {identifier(database)} OWNER {identifier(owner)} TEMPLATE template0')
        client.make_bucket(bucket)
        key,password=scoped_identity(client,admin,bucket)
        # 派生集合前缀与目标数据库绑定，和源索引彻底分离。
        knowledge_prefix,memory_prefix='restore_k_'+database,'restore_m_'+database
        environment={'POSTGRES_DB':database,'APP_MINIO_BUCKET':bucket,'APP_MINIO_ACCESS_KEY':key,'APP_MINIO_SECRET_KEY':password,
                     'RAG_COLLECTION_PREFIX':knowledge_prefix,'MEMORY_COLLECTION_PREFIX':memory_prefix}
        # 该文件包含新应用身份，权限0600、被gitignore排除，终端仅提示文件位置。
        state_volume='restore-'+database+'-sandbox-state';input_volume='restore-'+database+'-sandbox-inputs'
        write_json(root/'restore.compose.json',{'services':{'api':{'environment':environment},'worker':{'environment':environment},
            'sandbox-runner':{'environment':{'SANDBOX_INPUT_VOLUME':input_volume},
                'volumes':['restore-sandbox-state:/var/lib/sandbox','restore-sandbox-inputs:/var/lib/sandbox-inputs']}},
            'volumes':{'restore-sandbox-state':{'name':state_volume},'restore-sandbox-inputs':{'name':input_volume}}})
        await asyncio.to_thread(pg_command,settings,'pg_restore',['--dbname='+database,'--no-owner','--no-acl','--role='+owner,'--single-transaction','--exit-on-error',str(root/'database.dump')])
        conn=await connect_admin(target_settings)
        if await counts(conn)!=manifest['counts']:raise RuntimeError('恢复PG行数与原备份不一致，停止后续动作')
        for row in manifest['objects']:
            client.fput_object(bucket,row['key'],str(root/'objects'/row['file']),content_type=row['content_type'] or 'application/octet-stream')
            response=client.get_object(bucket,row['key']);value=hashlib.sha256()
            try:
                for block in response.stream(1024*1024):value.update(block)
            finally:response.close();response.release_conn()
            if value.hexdigest()!=row['sha256']:raise RuntimeError('恢复对象SHA不一致')
        await provision_roles(conn,target_settings);await grant_runtime_access(conn,target_settings)
        interrupted=await quarantine(conn,manifest['source_bucket'],bucket)
        runtime=Settings().model_copy(update={'postgres_db':database,'postgres_user':settings.app_db_user,'postgres_password':settings.app_db_password,
            'rag_collection_prefix':knowledge_prefix,'memory_collection_prefix':memory_prefix})
        indexes=await rebuild(conn,runtime)
        async with conn.transaction():
            await AuditRecorder(None,'maintenance').append(new_id(),'maintenance.restored',{'source_database':manifest['source_database'],
                'objects_verified':len(manifest['objects']),'interrupted_runs':interrupted,'indexes':indexes},connection=conn)
        receipt={'restore':'completed','database':database,'bucket':bucket,'objects_verified':len(manifest['objects']),'original_counts_verified':True,
                 'interrupted_runs':interrupted,'indexes':indexes,'redis':'new database namespace; rebuild from PG','configuration':'restore.compose.json'}
        write_json(root/'restore-receipt.json',receipt);print(json.dumps(receipt,ensure_ascii=False));return receipt
    finally:
        if conn:await conn.close()
        await control.close()


async def main():
    """读取CLI命令及目录，运行显式维护任务；拒绝把环境或凭证打印为诊断输出。"""
    parser=argparse.ArgumentParser(description='维护窗口备份/新目标恢复；运行前停止写入服务')
    parser.add_argument('command',choices=['backup','validate','restore'])
    parser.add_argument('--directory',default='/backup')
    parser.add_argument('--database');parser.add_argument('--bucket')
    args=parser.parse_args()
    if args.command=='validate':
        manifest=validate(args.directory);print(json.dumps({'valid':True,'objects':len(manifest['objects'])}));return
    settings=BootstrapSettings()
    if args.command=='backup':await backup(args.directory,settings)
    else:
        if not args.database or not args.bucket:parser.error('restore必须提供--database及--bucket')
        await restore(args.directory,settings,args.database,args.bucket)


if __name__=='__main__':asyncio.run(main())
