import { useEffect, useRef, useState } from 'react';
import { apiRequest, beijingTime, type SensitiveAction } from './api';

/** 接收敏感动作快照和决定回调，显示通用二次确认窗口；关闭不等于同意，不允许编辑执行载荷。 */
export function ActionConfirmation({action,decided}:{action:SensitiveAction;decided:()=>void}) {
  const dialog=useRef<HTMLDialogElement>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  useEffect(()=>{if(action.status==='pending' && !dialog.current?.open)dialog.current?.showModal();},[action.action_id,action.status]);
  /** 接收本次同意/拒绝选择，仅发送动作ID和决定；失败保持窗口，成功由后端恢复敏感操作流程。 */
  async function decide(decision:'approve'|'reject'){
    setBusy(true);setError('');
    try{await apiRequest(`/actions/${action.action_id}/decision`,'POST',{decision});dialog.current?.close();decided();}
    catch(reason){setError(reason instanceof Error ? reason.message : '无法保存确认决定');}
    finally{setBusy(false);}
  }
  const query=action.action_type==='business.query';
  return <div className="action-confirmation-entry"><p>敏感操作尚未执行，需要你确认。</p><button type="button" onClick={()=>dialog.current?.showModal()}>查看待确认动作</button>
    <dialog ref={dialog} className="action-confirmation" aria-labelledby={`action-title-${action.action_id}`}>
      <header><h2 id={`action-title-${action.action_id}`}>人工二次确认</h2><button type="button" disabled={busy} aria-label="关闭确认窗口" onClick={()=>dialog.current?.close()}>关闭</button></header>
      <p>{action.request.purpose}</p>
      {query ? <dl><dt>数据库 / Schema</dt><dd>{String(action.request.target.database)} / {String(action.request.target.schema)}</dd><dt>查询对象</dt><dd>{(action.request.target.tables as string[]).join('、')}</dd><dt>读取权限上限（实际筛选见下方SQL）</dt><dd>{String(action.request.target.scope)}</dd></dl> : <><h3>操作目标</h3><pre>{JSON.stringify(action.request.target,null,2)}</pre></>}
      <h3>{query ? '完整 SQL' : '完整待执行内容'}</h3><pre>{query ? String(action.request.payload.sql) : JSON.stringify(action.request.payload,null,2)}</pre>
      {query && <><h3>绑定参数</h3><pre>{JSON.stringify(action.request.payload.params,null,2)}</pre></>}
      <p>有效至 {beijingTime(action.expires_at)}。只允许本次操作；关闭窗口不会执行。</p>
      {error && <p className="error-text" role="alert">{error}</p>}
      <footer><button type="button" disabled={busy} onClick={()=>void decide('reject')}>拒绝</button><button type="button" className="primary-action" disabled={busy} onClick={()=>void decide('approve')}>{busy ? '正在保存…' : '允许本次操作'}</button></footer>
    </dialog>
  </div>;
}
