"""LangGraph异步检查点适配现有PG表；原始检查点追加保留，只有临时节点写入可幂等更新。"""
import base64

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP

from app.ids import new_id


class PgGraphSaver(BaseCheckpointSaver):
    """复用现有asyncpg连接池和检查点表，按服务端run绑定线程，不向浏览器暴露框架恢复接口。"""
    def __init__(self, pool, run_id):
        """接收应用池与可信run编号，初始化框架序列化器和线程边界，无数据库变更。"""
        super().__init__()
        self.pool,self.run_id=pool,run_id

    def packed(self, value):
        """接收框架对象，返回类型及Base64字节，完整保留interrupt及增量状态而不使用pickle。"""
        kind,data=self.serde.dumps_typed(value)
        return {'kind':kind,'data':base64.b64encode(data).decode()}

    def unpacked(self, value):
        """接收可信PG中保存的类型和字节，返回原框架状态。"""
        return self.serde.loads_typed((value['kind'],base64.b64decode(value['data'])))

    def config(self, graph_id=None, namespace=''):
        """接收框架检查点及命名空间，返回仅属于本run的内部定位配置。"""
        value={'thread_id':self.run_id,'checkpoint_ns':namespace}
        if graph_id:value['checkpoint_id']=graph_id
        return {'configurable':value}

    def checked(self, config):
        """接收框架配置，验证线程属于当前任务后返回定位字段；外来线程直接拒绝。"""
        values=config['configurable']
        if values['thread_id']!=self.run_id:raise ValueError('检查点不属于本次执行')
        return values

    async def aput(self, config, checkpoint, metadata, new_versions):
        """接收框架完整检查点、元数据及版本，在run锁内追加并返回新定位；不会删改旧记录。"""
        values=self.checked(config);namespace=values.get('checkpoint_ns','')
        payload={'namespace':namespace,'graph_id':checkpoint['id'],'parent':values.get('checkpoint_id'),
                 'checkpoint':self.packed(checkpoint),'metadata':self.packed(metadata)}
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('SELECT 1 FROM app_runtime.runs WHERE run_id=$1 FOR UPDATE',self.run_id)
                sequence=await conn.fetchval('SELECT COALESCE(max(sequence),0)+1 FROM app_runtime.checkpoints WHERE run_id=$1',self.run_id)
                await conn.execute('INSERT INTO app_runtime.checkpoints(checkpoint_id,run_id,sequence,state_json) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING',new_id(),self.run_id,sequence,payload)
        return self.config(checkpoint['id'],namespace)

    async def aput_writes(self, config, writes, task_id, task_path=''):
        """接收同一步的节点写入和任务ID，保存完整待处理结果/interrupt；特殊通道可幂等覆盖。"""
        values=self.checked(config)
        async with self.pool.acquire() as conn:
            for index,(channel,value) in enumerate(writes):
                position=WRITES_IDX_MAP.get(channel,index)
                suffix='DO UPDATE SET payload_json=EXCLUDED.payload_json' if position<0 else 'DO NOTHING'
                await conn.execute('INSERT INTO app_runtime.graph_writes(run_id,namespace,checkpoint_version,task_id,position,channel,payload_json) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (run_id,namespace,checkpoint_version,task_id,position) '+suffix,
                    self.run_id,values.get('checkpoint_ns',''),values['checkpoint_id'],task_id,position,channel,self.packed(value))

    async def aget_tuple(self, config):
        """接收当前或指定检查点定位，返回完整状态、父版本和待写入观察，支持跨worker恢复。"""
        values=self.checked(config);namespace=values.get('checkpoint_ns','');graph_id=values.get('checkpoint_id')
        async with self.pool.acquire() as conn:
            row=await conn.fetchrow("SELECT state_json FROM app_runtime.checkpoints WHERE run_id=$1 AND state_json->>'namespace'=$2 AND ($3::text IS NULL OR state_json->>'graph_id'=$3) ORDER BY sequence DESC LIMIT 1",self.run_id,namespace,graph_id)
            if row is None:return None
            state=row['state_json']
            writes=await conn.fetch('SELECT * FROM app_runtime.graph_writes WHERE run_id=$1 AND namespace=$2 AND checkpoint_version=$3 ORDER BY task_id,position',self.run_id,namespace,state['graph_id'])
        return CheckpointTuple(self.config(state['graph_id'],namespace),self.unpacked(state['checkpoint']),self.unpacked(state['metadata']),
            self.config(state['parent'],namespace) if state.get('parent') else None,
            [(row['task_id'],row['channel'],self.unpacked(row['payload_json'])) for row in writes])

    async def alist(self, config, *, filter=None, before=None, limit=None):
        """接收当前线程与历史过滤，异步产出其检查点；首期不提供公共时间旅行或删除接口。"""
        if config is None:config=self.config()
        values=self.checked(config)
        async with self.pool.acquire() as conn:
            rows=await conn.fetch("SELECT state_json FROM app_runtime.checkpoints WHERE run_id=$1 AND state_json->>'namespace'=$2 ORDER BY sequence DESC",self.run_id,values.get('checkpoint_ns',''))
        count=0
        for row in rows:
            item=await self.aget_tuple(self.config(row['state_json']['graph_id'],values.get('checkpoint_ns','')))
            if before and item.checkpoint['id']>=before['configurable']['checkpoint_id']:continue
            if filter and any(item.metadata.get(key)!=value for key,value in filter.items()):continue
            if limit is not None and count>=limit:break
            count+=1;yield item

    async def adelete_thread(self, thread_id):
        """接收线程编号，明确禁止删除永久检查点；不实现用户时间旅行/记录清理。"""
        raise PermissionError('永久执行检查点不能删除')
