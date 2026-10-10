import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { ArrowUp, Check, MessageSquare, Pencil, Square, X } from 'lucide-react';
import { apiRequest, beijingTime, requestId, citationPages, citationTitle, type ChatEvent, type ChatMessage, type ChatRun, type Conversation, type ConversationDetail, type Citation } from './api';
import { SourcePanel } from './SourcePanel';

interface Props { conversationId: string | null; revision: number; userName: string; select: (id: string) => void; changed: () => Promise<void>; }
const labels = {pending:'等待处理',running:'正在回答',completed:'回答完成',failed:'回答失败',cancelled:'已停止',interrupted:'执行中断'};

/** 接收本人所选会话与目录回调，渲染真实原消息、持久 SSE、停止和重命名，返回聊天工作区。 */
export function ChatWorkspace({conversationId,revision,userName,select,changed}: Props) {
  const [messages,setMessages] = useState<ChatMessage[]>([]);
  const [title,setTitle] = useState('新对话');
  const [draft,setDraft] = useState('');
  const [loading,setLoading] = useState(false);
  const [sending,setSending] = useState(false);
  const [stopping,setStopping] = useState(false);
  const [error,setError] = useState('');
  const [run,setRun] = useState<ChatRun | null>(null);
  const [liveText,setLiveText] = useState('');
  const [stage,setStage] = useState('');
  const [events,setEvents] = useState<ChatEvent[]>([]);
  const [renaming,setRenaming] = useState(false);
  const [newTitle,setNewTitle] = useState('');
  const [renameBusy,setRenameBusy] = useState(false);
  const [citation,setCitation] = useState<Citation | null>(null);
  const closeSource=useCallback(()=>setCitation(null),[]);
  // 同一条未成功确认的发送重用标识，网络重传不能生成第二个模型任务。
  const pendingKey = useRef<{content: string; conversation: string | null; key: string} | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const viewport = useRef<HTMLDivElement>(null);
  const busy = sending || run?.status==='pending' || run?.status==='running';

  /** 无参数从 PG 读取本人会话，返回资料，并同步原消息和当前任务，不信任浏览器历史内容。 */
  const refresh = useCallback(async () => {
    if (!conversationId) return;
    const detail = await apiRequest<ConversationDetail>(`/conversations/${conversationId}`);
    setTitle(detail.title); setMessages(detail.messages); setRun(detail.active_run || detail.last_run);
    return detail;
  },[conversationId]);

  useEffect(() => {
    let alive = true;
    setError(''); setMessages([]); setRun(null); setLiveText(''); setEvents([]); setStage(''); setRenaming(false); setDraft(''); setLoading(false);setCitation(null);
    pendingKey.current = null; pinned.current = true;
    if (!conversationId) { setTitle('新对话'); return; }
    setLoading(true);
    apiRequest<ConversationDetail>(`/conversations/${conversationId}`).then(detail => {
      if (alive) { setTitle(detail.title); setMessages(detail.messages); setRun(detail.active_run || detail.last_run); }
    }).catch(reason => { if (alive) setError(reason instanceof Error ? reason.message : '无法读取会话'); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  },[conversationId,revision]);

  useEffect(() => {
    if (!run) return;
    let closed = false;
    let lastSequence = 0;
    const historical = !['pending','running'].includes(run.status);
    // 原生 EventSource 自动携带 Cookie 并按 Last-Event-ID 重连；服务器只重放 PG 已保存的事件。
    const stream = new EventSource(`/api/runs/${run.run_id}/events`);
    stream.addEventListener('run.event',event => {
      const value = JSON.parse((event as MessageEvent).data) as ChatEvent;
      if (value.sequence<=lastSequence) return;
      lastSequence = value.sequence;
      setEvents(current => [...current.slice(-99),value]);
      if (value.type==='assistant.reset') setLiveText('');
      if (value.type==='assistant.delta') setLiveText(current => current+(value.data.text || ''));
      if (value.data.message) setStage(value.data.message);
      if (value.type==='run.started' && !historical) setRun(current => current ? {...current,status:'running'} : current);
      if (value.type.startsWith('run.') && value.data.status && !['pending','running'].includes(value.data.status)) {
        setRun(current => current ? {...current,status:value.data.status!,error:value.data.error} : current);
        setStopping(false); closed = true; stream.close();
        if (value.data.status==='completed') setStage('回答完成');
        void refresh().then(() => changed()).catch(reason => setError(reason instanceof Error ? reason.message : '无法恢复会话'));
      }
    });
    stream.addEventListener('stream.end',event => {
      const value = JSON.parse((event as MessageEvent).data) as ChatRun;
      closed = true; stream.close(); setStopping(false);
      if (value.status) setRun(current => current ? {...current,...value} : current);
      void refresh().catch(reason => setError(reason instanceof Error ? reason.message : '登录会话已失效，请刷新页面'));
    });
    stream.onerror = () => { if (!closed) setStage('连接暂时中断，正在恢复已保存的回答…'); };
    return () => { closed = true; stream.close(); };
  },[run?.run_id,refresh,changed]);

  useEffect(() => {
    if (pinned.current) bottom.current?.scrollIntoView({block:'end',behavior:'auto'});
  },[liveText,messages,events.length]);

  /** 接收输入表单事件，创建会话并持久发送；网络错误保留文本和同一幂等键，返回无假成功。 */
  async function send(event: FormEvent) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || busy) return;
    setSending(true); setError(''); setLiveText(''); setEvents([]); pinned.current = true;
    if (!pendingKey.current || pendingKey.current.content!==content || (conversationId!==null && pendingKey.current.conversation!==conversationId))
      pendingKey.current = {content,conversation:conversationId,key:requestId()};
    try {
      let id = conversationId || pendingKey.current.conversation;
      if (!id) {
        const conversation = await apiRequest<Conversation>('/conversations','POST',{});
        id = conversation.conversation_id;
        // 创建成功后保留 ID，发送确认丢失时重传同一会话，避免多次创建。
        pendingKey.current.conversation = id;
      }
      const next = await apiRequest<ChatRun>(`/conversations/${id}/messages`,'POST',{content,request_key:pendingKey.current.key});
      setDraft(''); pendingKey.current = null; setRun(next); setStage('消息已保存，等待处理');
      if (id!==conversationId) select(id);
      else await refresh();
      await changed();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '发送失败，文字已保留，请重试'); }
    finally { setSending(false); }
  }

  /** 接收键盘事件，Enter 发送、Shift+Enter 换行；输入法组合输入期间不会误发送。 */
  function onKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key==='Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); }
  }

  /** 无参数请求后台关闭本人当前模型流，保持事件连接直到实际终态，不只停止前端动画。 */
  async function stop() {
    if (!run || stopping) return;
    setStopping(true); setError('');
    try { await apiRequest(`/runs/${run.run_id}/cancel`,'POST'); }
    catch (reason) { setStopping(false); setError(reason instanceof Error ? reason.message : '无法停止本次回答'); }
  }

  /** 接收重命名表单事件，仅更新本人会话标题，成功后刷新目录，失败保留编辑内容。 */
  async function rename(event: FormEvent) {
    event.preventDefault();
    if (!conversationId || !newTitle.trim()) return;
    setRenameBusy(true); setError('');
    try { await apiRequest(`/conversations/${conversationId}`,'PATCH',{title:newTitle}); setTitle(newTitle.trim()); setRenaming(false); await changed(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '重命名失败'); }
    finally { setRenameBusy(false); }
  }

  return <div className={`chat-with-sources ${citation ? 'has-source' : ''}`}><div className="chat-workspace">
    <div className="chat-heading"><div><h1>{title}</h1><p>工作对话</p></div>
      {conversationId && <button className="rename-chat" aria-label="重命名会话" onClick={() => {setNewTitle(title);setRenaming(true);}}><Pencil size={18} />重命名</button>}</div>
    {renaming && <form className="rename-form" onSubmit={rename}><label htmlFor="conversation-title">会话名称</label><input id="conversation-title" autoFocus maxLength={120} value={newTitle} onChange={event => setNewTitle(event.target.value)} /><button disabled={renameBusy || !newTitle.trim()}><Check size={18} />保存</button><button type="button" disabled={renameBusy} onClick={() => setRenaming(false)}><X size={18} />取消</button></form>}
    <div className="chat-scroll" ref={viewport} onScroll={() => {const node=viewport.current;if(node)pinned.current=node.scrollHeight-node.scrollTop-node.clientHeight<80;}}>
      {loading ? <p role="status">正在读取已保存的会话…</p> : messages.length===0 && !busy ? <div className="chat-empty"><MessageSquare size={32} aria-hidden="true" /><h2>{userName}，从一个问题开始。</h2><p>讨论工作思路，或查询你有权访问的企业知识。<br />回答引用可打开原文，核实页码与依据。</p></div> : null}
      {messages.map(message => <article className={`chat-message ${message.role}`} key={message.message_id}>
        <div className="message-meta"><strong>{message.role==='user' ? '你' : '知行'}</strong><time dateTime={message.created_at}>{beijingTime(message.created_at)}</time></div>
        <div className="message-content">{message.content}</div>
        {!!message.citations?.length && <div className="message-sources" aria-label="回答引用">{message.citations.map(source=><button key={source.label} onClick={()=>setCitation(source)} aria-label={`来源 ${source.label} ${citationTitle(source)}`}>[{source.label}] {citationTitle(source)}{citationPages(source) ? ` · ${citationPages(source)}` : ''}</button>)}</div>}
        {message.finish_reason==='length' && <p className="message-limit-note">达到单次输出上限，可继续提问。</p>}
        {message.role==='user' && message.error && <p className="run-error">{message.error.message}</p>}
      </article>)}
      {liveText && run?.status!=='completed' && <article className="chat-message assistant live-reply"><div className="message-meta"><strong>知行</strong><span>{busy ? '正在回答' : '未完成的回答'}</span></div><div className="message-content">{liveText}</div></article>}
      {run && <div className={`chat-run-state ${run.status}`} role="status"><span className={busy || run.status==='completed' ? 'status-dot healthy' : 'status-dot'} />{busy ? stage || labels[run.status] : labels[run.status]}{run.error && <span>{run.error.message}</span>}</div>}
      {events.length>0 && <details className="chat-execution"><summary>查看本次执行记录</summary><ol>{events.filter(item=>item.type!=='assistant.delta' && item.type!=='assistant.reset').map(item=><li key={item.sequence}><time>{beijingTime(item.timestamp)}</time><span>{item.data.message || (item.type==='model.attempt_failed' ? '模型调用失败' : item.type==='run.queued' ? '等待处理' : item.type==='run.started' ? '开始处理' : item.type==='run.completed' ? '回答完成' : item.type==='run.cancelled' ? '已停止' : item.type)}</span></li>)}</ol></details>}
      <div ref={bottom} />
    </div>
    <form className="chat-composer-area" onSubmit={send}>
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="composer"><label className="sr-only" htmlFor="message">消息</label><textarea id="message" placeholder="向知行提问，或粘贴需要讨论的文字…" disabled={loading || sending} maxLength={16000} value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={onKey} />
        <div><span>Enter 发送，Shift+Enter 换行</span>{busy ? <button type="button" aria-label="停止回答" disabled={stopping || sending} onClick={() => void stop()}><Square size={18} /></button> : <button aria-label="发送消息" disabled={!draft.trim() || loading || sending}><ArrowUp size={20} /></button>}</div>
      </div>
    </form>
  </div>{citation && <SourcePanel key={citation.chunk_id} citation={citation} close={closeSource}/>}</div>;
}
