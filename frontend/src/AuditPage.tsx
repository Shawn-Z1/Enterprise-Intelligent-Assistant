import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ChevronDown, ChevronRight, CheckCircle2, Clock3, Copy, Download, FileText, RefreshCw, Search, ShieldCheck } from 'lucide-react';
import { apiRequest, beijingTime, knowledgeBlob } from './api';
import { MarkdownReply } from './MarkdownReply';

interface AuditConversation {conversation_id:string;title:string;user_id:string;username:string;display_name:string;department_name:string|null;updated_at:string;turns_count:number;failures:number;active_count:number;}
interface AuditMessage {message_id:string;content:string;created_at:string;truncated:boolean;attachments?:{attachment_id:string;filename:string;size_bytes:number}[];}
interface Turn {turn_number:number;message_order:number;run_id:string|null;trace_id:string|null;status:string;error:{message?:string}|null;user_message:AuditMessage;assistant_message:AuditMessage|null;}
interface Usage {span_id:string;phase:string;role:string;model:string|null;usage_source:string;usage_method:string|null;input_tokens:number|null;output_tokens:number|null;total_tokens:number|null;cached_tokens:number|null;duration_ms:number|null;}
interface Job {job_id:string;job_type:string;status:string;}
interface TurnDetail {usage:Usage[];background_pending:boolean;jobs:Job[];}
interface TimelineEvent {source:'audit'|'run_event';event_id:number;event_type:string;title:string;summary:string;group:'execution'|'background';warning:boolean;created_at:string;duration_ms:number|null;model:string|null;bytes:number;}
interface EventBody {data:unknown;too_large:boolean;bytes:number;}
const statuses:Record<string,string>={completed:'已完成',failed:'失败',running:'执行中',pending:'等待处理',queued:'排队中',waiting_sandbox:'等待沙箱',waiting_confirmation:'等待本人确认',cancelled:'已停止',interrupted:'已中断',recorded:'历史消息',unknown:'结果未知'};
const sources:Record<string,string>={reported:'供应商报告',estimated:'估算',unavailable:'未知'};

/** 接收管理员下载地址和文件名，校验响应后触发安全文件下载；失败交由调用方显示。 */
async function download(path:string,name:string) {
  const blob=await knowledgeBlob(path);const url=URL.createObjectURL(blob);
  const link=document.createElement('a');link.href=url;link.download=name;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}

/** 接收单条原消息，展示原文本/Markdown及附件；长消息按需读取全文，限额不冒充完整内容。 */
function MessageView({message,owner,assistant=false}:{message:AuditMessage;owner:string;assistant?:boolean}) {
  const [content,setContent]=useState(message.content);const [complete,setComplete]=useState(!message.truncated);
  const [busy,setBusy]=useState(false);const [error,setError]=useState('');
  /** 无参数读取这条持久原消息全文，过大改为受控导出，不改变聊天或触发工具。 */
  async function full() {setBusy(true);setError('');try{
    const value=await apiRequest<{content:string|null;too_large:boolean}>(`/admin/audit/messages/${message.message_id}`);
    if(value.too_large)await download(`/admin/audit/messages/${message.message_id}/export`,`message-${message.message_id}.json`);
    else {setContent(value.content||'');setComplete(true);}
  }catch(reason){setError(reason instanceof Error?reason.message:'消息读取失败');}finally{setBusy(false);}}
  return <div className={`audit-chat-message ${assistant?'assistant':'user'}`}>
    <span className="audit-avatar">{assistant?'知':owner.slice(0,1)}</span><div className="audit-message-main"><strong>{assistant?'知行':owner}</strong>
      <div className="audit-message-body">{assistant?<MarkdownReply content={content}/>:<p>{content}</p>}</div>
      {!!message.attachments?.length&&<div className="audit-attachments">{message.attachments.map(file=><span key={file.attachment_id} title={file.filename}><FileText size={14}/>{file.filename}<small>{Math.ceil(file.size_bytes/1024)} KiB</small></span>)}</div>}
      {!complete&&<div className="audit-note">当前显示前 12,000 字符。<button disabled={busy} onClick={()=>void full()}>{busy?'正在读取…':'读取完整消息 / 超限下载'}</button></div>}
      {error&&<p role="alert" className="error-text">{error}</p>}</div>
  </div>;
}

/** 接收事件元数据，点击本行即在下方读取/收起原JSON；每个事件各自独立，禁止执行正文代码。 */
function EventRow({event,number}:{event:TimelineEvent;number:number}) {
  const [open,setOpen]=useState(false);const [body,setBody]=useState<EventBody|null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [copied,setCopied]=useState(false);
  useEffect(()=>{let alive=true;if(!open||body)return;setBusy(true);setError('');
    apiRequest<EventBody>(`/admin/audit/${event.source==='audit'?'events':'run-events'}/${event.event_id}`).then(value=>{if(alive)setBody(value);})
      .catch(reason=>{if(alive)setError(reason instanceof Error?reason.message:'事件读取失败');}).finally(()=>{if(alive)setBusy(false);});
    return ()=>{alive=false;};
  },[open,body,event.source,event.event_id]);
  /** 无参数复制已读取的凭证遮蔽JSON，复制失败直接提示，不读取额外秘密。 */
  async function copy() {try{await navigator.clipboard.writeText(JSON.stringify(body?.data,null,2));setCopied(true);}catch{setError('复制失败，请选中文本复制');}}
  /** 无参数下载实际单事件档案，继续受管理员检查和服务端限额约束。 */
  async function exportEvent() {try{await download(`/admin/audit/events/${event.event_id}/export`,`event-${event.event_id}.json`);}catch(reason){setError(reason instanceof Error?reason.message:'导出失败');}}
  const failed=event.warning||/(failed|rejected|denied|unknown|interrupted)$/.test(event.event_type);const bodyId=`audit-event-${event.source}-${event.event_id}`;
  return <li className={`audit-event-row ${open?'open':''}`}><button className="audit-event-toggle" aria-expanded={open} aria-controls={bodyId} onClick={()=>setOpen(value=>!value)}>
    {open?<ChevronDown size={16}/>:<ChevronRight size={16}/>}<span className={`audit-event-status ${failed?'warning':''}`}>{failed?<Clock3 size={15}/>:<CheckCircle2 size={15}/>}</span>
    <span className="audit-step-number">{String(number).padStart(2,'0')}</span><strong>{event.title}</strong>
    <small className="audit-event-summary">{event.summary||event.model||beijingTime(event.created_at)}</small><time>{event.duration_ms==null?'—':`${(event.duration_ms/1000).toFixed(2)}秒`}</time>
  </button>{open&&<div id={bodyId} className="audit-inline-json"><div className="audit-json-toolbar"><span>{event.event_type} · {beijingTime(event.created_at)} · #{event.event_id}</span>
    {body&&!body.too_large&&<button onClick={()=>void copy()}><Copy size={14}/>{copied?'已复制':'复制 JSON'}</button>}
    {event.source==='audit'&&<button onClick={()=>void exportEvent()}><Download size={14}/>导出事件</button>}<button onClick={()=>setOpen(false)}>收起</button></div>
    {busy&&<p role="status">正在读取 JSON…</p>}{error&&<p role="alert" className="error-text">{error}</p>}
    {body&&(body.too_large?<p>正文超过 256KiB 页面限额，完整原记录仍保留。{event.source==='audit'?'可导出单事件查看。':'请通过受控数据库管理渠道查看。'}</p>:<pre>{JSON.stringify(body.data,null,2)}</pre>)}
  </div>}</li>;
}

/** 接收单轮run和刷新版本，读取本轮模型事实与中文事件；确认/后台作业按明确用途展示。 */
function TurnExecution({runId,traceId,revision}:{runId:string;traceId:string;revision:number}) {
  const [detail,setDetail]=useState<TurnDetail|null>(null);const [events,setEvents]=useState<TimelineEvent[]>([]);const [page,setPage]=useState(1);const [chunks,setChunks]=useState(false);const [more,setMore]=useState(false);
  const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [exportBusy,setExportBusy]=useState(false);const [exportAfter,setExportAfter]=useState('0');const [exportLimit,setExportLimit]=useState('500');const [exportNotice,setExportNotice]=useState('');
  useEffect(()=>{let alive=true;
    apiRequest<TurnDetail>(`/admin/audit/runs/${runId}`).then(value=>{if(alive)setDetail(value);}).catch(reason=>{if(alive)setError(reason instanceof Error?reason.message:'模型用量不可读取');});
    return ()=>{alive=false;};
  },[runId,revision]);
  useEffect(()=>{let alive=true;setBusy(true);setError('');
    apiRequest<{items:TimelineEvent[];has_more:boolean}>(`/admin/audit/runs/${runId}/events?page=${page}&limit=50&chunks=${chunks}`)
      .then(value=>{if(alive){setEvents(value.items);setMore(value.has_more);}}).catch(reason=>{if(alive)setError(reason instanceof Error?reason.message:'执行过程读取失败');}).finally(()=>{if(alive)setBusy(false);});
    return ()=>{alive=false;};
  },[runId,page,chunks,revision]);
  /** 无参数导出本轮真实审计事件，展示完整性和下一游标；不把分批导出当作全任务档案。 */
  async function exportRound() {setExportBusy(true);setExportNotice('');try{
    const after=Number(exportAfter),limit=Number(exportLimit);if(!Number.isInteger(after)||after<0||!Number.isInteger(limit)||limit<1||limit>1000)throw new Error('游标需非负，导出条数需 1–1000');
    const blob=await knowledgeBlob(`/admin/audit/traces/${traceId}/export?after=${after}&limit=${limit}`);
    const value=JSON.parse(await blob.text()) as {complete:boolean;next_after:number|null;events:unknown[]};
    const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=`round-${runId}-${after}.json`;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    setExportNotice(value.complete?`已导出查询时的完整审计事件（${value.events.length} 条）。`:`已导出 ${value.events.length} 条，本批不是完整档案。${value.next_after?`下一游标：${value.next_after}`:'此前事件不在本批中。'}`);
  }catch(reason){setError(reason instanceof Error?reason.message:'导出失败');}finally{setExportBusy(false);}}
  const main=events.filter(item=>item.group==='execution');const background=events.filter(item=>item.group==='background');
  /** 接收同页事件子集，保留真实排序编号，返回内联折叠列表。 */
  function eventList(items:TimelineEvent[]) {return <ol className="audit-timeline">{items.map(event=><EventRow key={`${event.source}:${event.event_id}`} event={event} number={(page-1)*50+events.indexOf(event)+1}/>)}</ol>;}
  return <div className="audit-turn-execution">
    {error&&<p role="alert" className="error-text">{error}</p>}
    <div className="audit-usage-section"><h3>本轮模型用量与耗时</h3><div className="audit-table"><table><thead><tr><th>环节</th><th>模型 / 来源</th><th>输入 Token</th><th>输出 Token</th><th>总 Token</th><th>缓存命中</th><th>耗时</th></tr></thead><tbody>
      {detail?.usage.map(row=><tr key={row.span_id}><td>{row.phase}</td><td>{row.model||'未记录型号'}<small>{sources[row.usage_source]||row.usage_source}</small>{row.usage_method&&<small>{row.usage_method}</small>}</td>
        <td>{row.input_tokens??'—'}</td><td>{row.output_tokens??'—'}</td><td>{row.total_tokens??'—'}</td><td>{row.cached_tokens??'—'}</td><td>{row.duration_ms==null?'—':`${(row.duration_ms/1000).toFixed(2)}秒`}</td></tr>)}
    </tbody></table></div>{detail&&!detail.usage.length&&<p className="audit-note">没有可得的模型用量记录；未知不等于零。</p>}</div>
    <div className="audit-event-heading"><h3>本轮执行过程</h3><label><input type="checkbox" checked={chunks} onChange={e=>{setChunks(e.target.checked);setPage(1);}}/>显示流式原始包</label>
      <button className="audit-outline-button" onClick={()=>void exportRound()} disabled={exportBusy}><Download size={15}/>{exportBusy?'正在导出…':'导出本轮记录'}</button></div>
    <p className="audit-note">点击事件即可在下方查看原始 JSON。默认折叠流式分片，主流程与后台记录分别展示。</p>
    {busy&&<p role="status">正在读取执行过程…</p>}{eventList(main)}{!!background.length&&<div className="audit-background"><h4>后台记忆处理</h4><p className="audit-note">可能在回答后继续，也可能与主流程并行；时间以原记录为准。</p>{eventList(background)}</div>}
    {!!detail?.jobs.length&&<div className="audit-job-status"><strong>本轮关联的记忆作业</strong>{detail.jobs.map(job=><span key={job.job_id}>{job.job_type==='extract'?'提取 / 整理':job.job_type==='index'?'索引更新':job.job_type}：{statuses[job.status]||job.status}</span>)}</div>}
    {detail?.background_pending&&<p className="audit-note">后台作业尚未结束，可点击页面上方“刷新记录”查看后续结果。</p>}
    <div className="audit-event-pagination"><button disabled={page===1||busy} onClick={()=>setPage(value=>value-1)}>前一批事件</button><span>第 {page} 批 · 每批最多 50 条</span><button disabled={!more||busy} onClick={()=>setPage(value=>value+1)}>后一批事件</button></div>
    <details className="audit-export-options"><summary>分批导出设置</summary><div><label>事件游标<input type="number" min="0" value={exportAfter} onChange={e=>setExportAfter(e.target.value)}/></label><label>每批条数<input type="number" min="1" max="1000" value={exportLimit} onChange={e=>setExportLimit(e.target.value)}/></label></div>
      <p>导出永久审计原事件，最多 1000 条 / 16MiB；运行进度正文可在上方对应行读取。分批文件不能当作完整任务档案。</p></details>
    {exportNotice&&<p role="status" className="audit-note">{exportNotice}</p>}
  </div>;
}

/** 接收一来一回的真实问答，先显示问题/回复，再按需展开这一轮的执行档案。 */
function TurnCard({turn,owner,revision}:{turn:Turn;owner:string;revision:number}) {
  const [open,setOpen]=useState(false);const bodyId=`audit-turn-${turn.user_message.message_id}`;
  return <article className="audit-turn-card"><header><strong>第 {turn.turn_number} 轮 · {beijingTime(turn.user_message.created_at)}</strong><span className={`audit-status ${turn.status==='completed'?'complete':''}`}>{statuses[turn.status]||turn.status}</span>
    <button disabled={!turn.run_id} aria-expanded={open} aria-controls={bodyId} onClick={()=>setOpen(value=>!value)}>{open?'收起本轮详情':turn.run_id?'查看本轮详情':'历史消息无执行档案'}{open?<ChevronDown size={16}/>:<ChevronRight size={16}/>}</button></header>
    <MessageView message={turn.user_message} owner={owner}/>
    {turn.assistant_message?<MessageView message={turn.assistant_message} owner={owner} assistant/>:<p className="audit-no-reply">{turn.error?.message||(['pending','running','waiting_sandbox','waiting_confirmation'].includes(turn.status)?'本轮尚未生成最终回复。':'本轮没有已保存的 Agent 回复。')}</p>}
    {open&&turn.run_id&&turn.trace_id&&<div id={bodyId}><TurnExecution runId={turn.run_id} traceId={turn.trace_id} revision={revision}/></div>}
  </article>;
}

/** 接收完整会话，逐批读取全部问答；切换时卸载旧轮次，避免迟到响应串会话。 */
function ConversationView({id,revision}:{id:string;revision:number}) {
  const [conversation,setConversation]=useState<AuditConversation|null>(null);const [turns,setTurns]=useState<Turn[]>([]);const [after,setAfter]=useState(0);const [next,setNext]=useState<number|null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState('');
  useEffect(()=>{setAfter(0);},[revision]);
  useEffect(()=>{let alive=true;setBusy(true);setError('');
    apiRequest<{conversation:AuditConversation;items:Turn[];next_after:number|null}>(`/admin/audit/conversations/${id}/turns?after=${after}&limit=20`)
      .then(value=>{if(alive){setConversation(value.conversation);setTurns(previous=>after?([...previous,...value.items].filter((item,index,all)=>all.findIndex(row=>row.user_message.message_id===item.user_message.message_id)===index)):value.items);setNext(value.next_after);}})
      .catch(reason=>{if(alive)setError(reason instanceof Error?reason.message:'会话读取失败');}).finally(()=>{if(alive)setBusy(false);});
    return ()=>{alive=false;};
  },[id,after,revision]);
  return <section className="audit-conversation-detail">{conversation&&<header><h2>会话详情</h2><h3>{conversation.title}</h3><p>{conversation.display_name}（{conversation.username}） · {conversation.department_name||'未分配部门'} · {conversation.turns_count} 轮问答</p><small>会话编号：{conversation.conversation_id}</small></header>}
    {error&&<p role="alert" className="error-text">{error}</p>}{turns.map(turn=><TurnCard key={turn.user_message.message_id} turn={turn} owner={conversation?.display_name||'用户'} revision={revision}/>)}
    {busy&&<p role="status">正在读取会话…</p>}{!busy&&!turns.length&&<p className="audit-empty">此会话尚未发送消息。</p>}
    {next!==null&&<button className="audit-outline-button audit-load-turns" disabled={busy} onClick={()=>setAfter(next)}>继续读取后续问答</button>}
  </section>;
}

/** 无参数渲染会话审计；页容量随可用高度变化，保持最左侧个人历史导航完全不变。 */
export function AuditPage() {
  const [filters,setFilters]=useState({user:'',search:'',since:'',until:'',status:''});const [query,setQuery]=useState('');const [page,setPage]=useState(1);const [limit,setLimit]=useState(7);const [revision,setRevision]=useState(0);
  const [rows,setRows]=useState<AuditConversation[]>([]);const [total,setTotal]=useState(0);const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [selected,setSelected]=useState('');
  // 固定可用区域按至少90px卡片及10px间隔选择容量，内容不改变观察高度；容量变动回到首页。
  const cards=useRef<HTMLDivElement>(null);const observedLimit=useRef(7);
  useEffect(()=>{const node=cards.current;if(!node)return;const observer=new ResizeObserver(entries=>{
    const height=entries[0].contentRect.height;if(height<80)return;const count=Math.max(3,Math.min(12,Math.floor((height+10)/100)));
    if(observedLimit.current!==count){observedLimit.current=count;setLimit(count);setPage(1);}
  });observer.observe(node);return ()=>observer.disconnect();},[]);
  useEffect(()=>{let alive=true;setBusy(true);setError('');
    apiRequest<{items:AuditConversation[];total:number}>(`/admin/audit/conversations?${query}&page=${page}&limit=${limit}`)
      .then(value=>{if(alive){setRows(value.items);setTotal(value.total);}}).catch(reason=>{if(alive){setRows([]);setError(reason instanceof Error?reason.message:'会话列表读取失败');}}).finally(()=>{if(alive)setBusy(false);});
    return ()=>{alive=false;};
  },[query,page,limit,revision]);
  /** 接收筛选事件，以北京时间编码参数提交；清理旧选择，不把旧详情解释成新筛选结果。 */
  function search(event:FormEvent) {event.preventDefault();const params=new URLSearchParams();for(const [key,value] of Object.entries(filters))if(value)params.set(key,key==='since'||key==='until'?`${value}:00+08:00`:value);setQuery(params.toString());setPage(1);setSelected('');setRevision(value=>value+1);}
  /** 无参数重置审计筛选/分页，不影响个人历史及正常聊天选中状态。 */
  function reset() {setFilters({user:'',search:'',since:'',until:'',status:''});setQuery('');setPage(1);setSelected('');setRevision(value=>value+1);}
  return <section className="audit-page"><header className="audit-heading"><div><h1><ShieldCheck size={23}/>调试与审计</h1><p>按会话查看每轮问答与执行过程 · 仅管理员可访问</p></div><button className="audit-outline-button" onClick={()=>setRevision(value=>value+1)} disabled={busy}><RefreshCw size={16}/>刷新记录</button></header>
    <form className="audit-filters" onSubmit={search}><label>用户<input placeholder="全部用户 / 姓名 / 账号" value={filters.user} onChange={e=>setFilters({...filters,user:e.target.value})}/></label>
      <label className="audit-search-field">会话搜索<div><Search size={16}/><input placeholder="会话标题或编号" value={filters.search} onChange={e=>setFilters({...filters,search:e.target.value})}/></div></label>
      <label>开始时间（北京）<input type="datetime-local" value={filters.since} onChange={e=>setFilters({...filters,since:e.target.value})}/></label><label>结束时间（北京）<input type="datetime-local" value={filters.until} onChange={e=>setFilters({...filters,until:e.target.value})}/></label>
      <label>状态<select value={filters.status} onChange={e=>setFilters({...filters,status:e.target.value})}><option value="">全部</option><option value="failed">有失败轮次</option><option value="active">有处理中轮次</option></select></label>
      <button className="audit-query-button" disabled={busy}>查询</button><button className="audit-outline-button" type="button" onClick={reset}>重置</button></form>
    {error&&<p role="alert" className="error-text">{error}</p>}
    <div className="audit-columns"><aside className="audit-session-panel"><header><h2>会话列表</h2><small>每页 {limit} 个</small></header>
      <div className="audit-session-cards" ref={cards} style={{gridTemplateRows:`repeat(${limit},minmax(90px,1fr))`}}>{rows.map(row=><button className={`audit-session ${selected===row.conversation_id?'active':''}`} key={row.conversation_id} title={row.title} aria-pressed={selected===row.conversation_id} onClick={()=>setSelected(row.conversation_id)}>
        <strong>{row.title}<ChevronRight size={16}/></strong><span>{row.display_name} · {row.department_name||row.username}</span><div><span>{row.turns_count} 轮问答{row.active_count?' · 处理中':row.failures?` · ${row.failures} 轮失败`:''}</span><time>{beijingTime(row.updated_at)}</time></div>
      </button>)}{busy&&!rows.length&&<p role="status">正在读取会话列表…</p>}{!busy&&!rows.length&&<p>没有符合条件的会话。</p>}</div>
      <footer><span>共 {total} 个会话 · 第 {page} / {Math.max(1,Math.ceil(total/limit))} 页</span><div><button disabled={page<=1||busy} onClick={()=>setPage(value=>value-1)}>上一页</button><button disabled={page*limit>=total||busy} onClick={()=>setPage(value=>value+1)}>下一页</button></div></footer>
    </aside>{selected?<ConversationView key={selected} id={selected} revision={revision}/>:<div className="audit-conversation-detail"><h2>会话详情</h2><p className="audit-empty">选择左侧会话，查看用户消息、Agent 回复及每轮执行过程。</p></div>}</div>
  </section>;
}
