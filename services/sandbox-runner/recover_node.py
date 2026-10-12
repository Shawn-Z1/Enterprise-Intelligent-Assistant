"""可信运维入口：只清理一个已经失联的Runner归属，不执行用户代码，不清理活节点。"""
import asyncio
import os
import httpx
import server


async def recover():
    """无参数读取明确的旧节点名，复检过期状态并独占清理；确认客体销毁后才释放未知任务额度。"""
    if not os.environ.get('SANDBOX_RUNNER_ID'):raise RuntimeError('必须明确指定待恢复的旧Runner身份')
    async with httpx.AsyncClient(timeout=15,trust_env=False) as api:
        response=await api.get(server.API_URL+'/status',headers=server.HEADERS);response.raise_for_status()
        node=next((row for row in response.json()['nodes'] if row['runner_id']==server.RUNNER_ID),None)
        if not node or node['live']:raise RuntimeError('旧节点不存在或仍在心跳，拒绝清理')
        server.node_manifest=await server.manifest()
        await server.register(api,True)
        await asyncio.to_thread(server.cleanup_owned)
        await server.register(api,True,cleaned=True)
        response=await api.post(server.API_URL+'/nodes/stopped',headers=server.HEADERS,json={'runner_id':server.RUNNER_ID,'instance_id':server.INSTANCE_ID})
        response.raise_for_status()
        print('已清理指定失联节点；未知任务保留未知状态，不重跑代码。')


if __name__=='__main__':
    asyncio.run(recover())
