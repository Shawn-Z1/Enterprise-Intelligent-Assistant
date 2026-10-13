import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { Activity, ArrowRight, ArrowUp, BookOpen, Check, ChevronRight, CircleHelp,
  Eye, EyeOff, FileText, Menu, MessageSquare, Plus, RefreshCw, Search,
  ShieldCheck, UserRound, UsersRound, Workflow, X } from 'lucide-react';
import { apiRequest, ApiError, beijingTime, fetchEvents, fetchReadiness, type Conversation, type Readiness, type RunEvent, type UserProfile } from './api';
import { PersonalCenter, StaffManagement } from './IdentityPages';
import { ChatWorkspace } from './ChatWorkspace';
import { KnowledgePage } from './KnowledgePage';
import { AuditPage } from './AuditPage';

type Page = 'conversation' | 'knowledge' | 'automation' | 'profile' | 'staff' | 'audit';
const pages = { conversation: '新建对话', knowledge: '知识库管理', automation: '自动化任务流', profile: '个人中心', staff: '职员管理', audit: '调试与审计' };

/** 无参数渲染文字品牌标识；返回中文标记与产品名，不包含交互。 */
function Brand() {
  return <div className="brand"><span className="brand-mark" aria-hidden="true">知</span>
    <span>知行<span className="brand-caption">企业智能助理</span></span></div>;
}

/** 无参数渲染真实身份与流式会话工作台；会话、原消息和权限均从后端读取。 */
export default function App() {
  // 身份只取自后端 Cookie 会话查询，不从本地存储或浏览器角色字段恢复。
  const [user,setUser] = useState<UserProfile | null>(null);
  const [authLoading,setAuthLoading] = useState(true);
  const [authBusy,setAuthBusy] = useState(false);
  const [authError,setAuthError] = useState('');
  const [view, setView] = useState(location.pathname === '/login' ? 'login' : 'workspace');
  const [page, setPage] = useState<Page>('conversation');
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  // 依赖状态完全来自真实就绪接口，空值代表尚未获取或连接失败。
  const [health, setHealth] = useState<Readiness | null>(null);
  const [healthError, setHealthError] = useState('');
  const [checking, setChecking] = useState(false);
  // 这里只展示基础设施追踪，不代表用户执行记录或永久审计。
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [eventsError, setEventsError] = useState('');
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [registration, setRegistration] = useState(false);
  const [historyRows,setHistoryRows] = useState<Conversation[]>([]);
  const [historyError,setHistoryError] = useState('');
  const [historyQuery,setHistoryQuery] = useState('');
  const [conversationId,setConversationId] = useState<string | null>(location.pathname.match(/^\/conversations\/([A-Za-z0-9_-]{1,16})$/)?.[1] || null);
  const [chatRevision,setChatRevision] = useState(0);

  /** 无参数读取本人历史目录；失败显示中文提示，不从浏览器缓存拼接其他用户数据。 */
  const refreshHistory = useCallback(async () => {
    if (!user) return;
    try { setHistoryRows(await apiRequest<Conversation[]>('/conversations')); setHistoryError(''); }
    catch (reason) { setHistoryError(reason instanceof Error ? reason.message : '无法读取历史会话'); }
  },[user?.user_id]);

  useEffect(() => { if (user) void refreshHistory(); else if (!authLoading) {setHistoryRows([]);setConversationId(null);} },[user?.user_id,refreshHistory,authLoading]);

  /** 接收会话编号，切换本人历史并保存可刷新的路径，同时收起小屏导航。 */
  function openConversation(id: string) { setConversationId(id);setPage('conversation');setSidebarOpen(false);history.pushState({},'',`/conversations/${id}`); }

  /** 无参数开始空白对话；首次发送才建立数据库会话，已有后台任务继续保存。 */
  function newConversation() { setConversationId(null);setChatRevision(value=>value+1);setPage('conversation');setSidebarOpen(false);history.pushState({},'','/'); }

  /** 无参数刷新当前服务端身份；失效时回到登录，权限变化后隐藏不再允许的入口。 */
  const refreshUser = useCallback(async () => {
    try { const profile = await apiRequest<UserProfile>('/auth/me'); setUser(profile); setView('workspace'); setAuthError('');
      if (location.pathname === '/login') history.replaceState({},'', '/');
      if (profile.role !== 'admin') setPage(current => current === 'staff' || current === 'audit' ? 'conversation' : current); }
    catch (error) { if (error instanceof ApiError && error.status === 401) { setUser(null); setView('login'); history.replaceState({},'', '/login'); }
      else { setAuthError(error instanceof Error ? error.message : '无法读取会话'); } }
    finally { setAuthLoading(false); }
  },[]);

  useEffect(() => {
    void refreshUser();
    const onFocus = () => { void refreshUser(); };
    addEventListener('focus',onFocus);
    return () => removeEventListener('focus',onFocus);
  },[refreshUser]);

  /** 接收真实账号表单；登录或零权限注册完成后读取本人身份，再进入工作台。 */
  async function submitAuth(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setAuthBusy(true); setAuthError('');
    const fields = new FormData(event.currentTarget);
    try { await apiRequest(registration ? '/auth/register' : '/auth/login','POST',{
      username: fields.get('username'), password: fields.get('password'), ...(registration ? {display_name:fields.get('display_name')} : {}) });
      await refreshUser(); navigate('workspace'); }
    catch (error) { setAuthError(error instanceof Error ? error.message : '认证失败，请稍后重试'); }
    finally { setAuthBusy(false); }
  }

  /** 无参数撤销真实会话，成功或已失效后返回登录；存储失败不显示假退出成功。 */
  async function logout() {
    try { await apiRequest('/auth/logout','POST'); }
    catch (error) { if (!(error instanceof ApiError && error.status === 401)) { setAuthError(error instanceof Error ? error.message : '退出失败'); throw error; } }
    setUser(null); setPage('conversation'); setView('login'); history.replaceState({},'', '/login');
  }

  /** 参数 signal 用于组件卸载取消；读取实际后端状态并更新界面，无返回值。 */
  const refreshHealth = useCallback(async (signal?: AbortSignal) => {
    setChecking(true);
    setHealthError('');
    try { setHealth(await fetchReadiness(signal)); }
    catch (error) {
      if (!signal?.aborted) { setHealth(null); setHealthError(error instanceof Error ? error.message : '检查失败'); }
    } finally { if (!signal?.aborted) setChecking(false); }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refreshHealth(controller.signal);
    return () => controller.abort();
  }, [refreshHealth]);

  /** 参数 target 为预览目标；切换页面并同步路径，不执行登录或提权。 */
  function navigate(target: 'login' | 'workspace') {
    setView(target);
    history.pushState({}, '', target === 'login' ? '/login' : '/');
  }

  useEffect(() => {
    /** 无参数响应浏览器前进后退；按当前路径恢复页面壳，无返回值。 */
    function restoreView() { setView(location.pathname === '/login' ? 'login' : 'workspace');setConversationId(location.pathname.match(/^\/conversations\/([A-Za-z0-9_-]{1,16})$/)?.[1] || null);setPage('conversation'); }
    addEventListener('popstate', restoreView);
    return () => removeEventListener('popstate', restoreView);
  }, []);

  /** 无参数打开运行状态面板并读取实际事件；失败保留明确说明，不制造事件。 */
  async function openStatus() {
    setPanelOpen(true);
    setEventsError('');
    try { setEvents(await fetchEvents()); }
    catch (error) { setEventsError(error instanceof Error ? error.message : '事件读取失败'); }
  }

  /** 参数 target 为常规模块；更新选中入口并收起小屏导航，无业务副作用。 */
  function selectPage(target: Page) { setPage(target); setSidebarOpen(false); }

  const statusLabel = checking ? '正在检查连接' : healthError ? '服务连接失败' : health?.status === 'ready' ? '基础服务已就绪' : '基础服务未就绪';

  const statusPanel = panelOpen && <aside className="status-panel" aria-label="基础服务运行状态">
    <div className="panel-heading"><h2>运行状态</h2><button aria-label="关闭运行状态" onClick={() => setPanelOpen(false)}><X size={20} /></button></div>
    <p className="muted">来自后端的实际依赖检查</p>
    <button className="refresh-button" disabled={checking} onClick={() => void refreshHealth()}><RefreshCw size={16} className={checking ? 'spin' : ''} />{checking ? '检查中…' : '重新检查连接'}</button>
    <p className="status-detail" role="status">{statusLabel}</p>
    {healthError && <p className="error-text">{healthError}</p>}
    {health && Object.entries(health.dependencies).map(([name, dependency]) => <div className="dependency" key={name}>
      <div><strong>{name === 'postgres' ? 'PostgreSQL' : name === 'minio' ? 'MinIO' : name === 'redis' ? 'Redis' : name === 'retrieval_models' ? '检索模型' : 'Milvus'}</strong>
        <span className={dependency.status === 'ready' ? 'healthy-text' : 'error-text'}>{dependency.status === 'ready' ? '已连通' : '不可用'}</span></div>
      <code>{dependency.target}</code><p>{dependency.detail}</p><small>{dependency.latency_ms} ms</small>
    </div>)}
    <h3>基础事件</h3><p className="muted small">本面板显示当前进程最近 100 条基础事件。永久审计已存入 PostgreSQL，管理员查询页面将在后续开放。</p>
    {eventsError && <p className="error-text">{eventsError}</p>}
    <div className="event-list">{events.slice(-6).reverse().map(event => <div key={event.event_id}>
      <span>#{event.event_id} · {new Date(event.timestamp).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai' })}</span>
      <strong>{event.type}</strong><code title={event.trace_id}>{event.trace_id}</code>
    </div>)}</div>
  </aside>;

  if (authLoading) return <div className="auth-loading"><Brand /><p role="status">正在恢复登录状态…</p></div>;
  if (!user || view === 'login') return <div className="login-layout">
    <section className="login-story"><Brand />
      <div className="login-intro"><h1>企业知识与业务，<br />在一个工作台处理。</h1><p>查询企业资料、分析业务数据、整理会议记录。<br />使用企业账号，进入你的工作空间。</p>
      <div className="story-features">
        <div><BookOpen size={24} aria-hidden="true" /><span><strong>企业知识</strong><p>制度、流程与项目资料，回答附有来源。</p></span></div>
        <div><Activity size={24} aria-hidden="true" /><span><strong>业务数据</strong><p>在权限范围内查询，执行前由本人确认。</p></span></div>
        <div><FileText size={24} aria-hidden="true" /><span><strong>记录与沉淀</strong><p>整理对话和会议文字，提交后审核入库。</p></span></div>
      </div></div>
      <div className="story-note"><ShieldCheck size={20} aria-hidden="true" /><span>可信身份、受控访问、操作可追溯</span></div>
    </section>
    <section className="login-form-area"><div className="login-form-wrap"><span className="stage-badge">账号登录</span>
      <h2>{registration ? '创建账号' : '欢迎来到知行'}</h2><p className="muted">{registration ? '注册后由管理员分配业务权限' : '使用企业账号，开启你的工作空间'}</p>
      <form onSubmit={submitAuth}>
        <label htmlFor="account">账号</label><input id="account" name="username" autoComplete="username" placeholder="英文、数字、下划线或连字符" minLength={3} maxLength={32} pattern="[A-Za-z0-9_-]+" required />
        {registration && <><label htmlFor="register-name">姓名</label><input id="register-name" name="display_name" autoComplete="name" maxLength={80} required /></>}
        <label htmlFor="password">密码</label><div className="password-input"><input id="password" name="password" type={passwordVisible ? 'text' : 'password'} autoComplete={registration ? 'new-password' : 'current-password'} minLength={registration ? 12 : undefined} maxLength={128} required placeholder={registration ? '至少12位' : '请输入密码'} />
          <button type="button" aria-label={passwordVisible ? '隐藏密码' : '显示密码'} onClick={() => setPasswordVisible(!passwordVisible)}>{passwordVisible ? <EyeOff size={18} /> : <Eye size={18} />}</button></div>
        <p className="form-notice" id="auth-notice">{registration ? '注册仅创建普通账号，部门、职级和业务权限由管理员分配。' : '使用管理员分配的账号或注册普通账号。'}</p>
        {authError && <p className="error-text" role="alert">{authError}</p>}
        <button className="primary-button" disabled={authBusy} aria-describedby="auth-notice">{authBusy ? '正在处理…' : registration ? '创建账号并进入' : '登录'}</button>
      </form>
      <p className="switch-form">{registration ? '已有账号？' : '还没有账号？'}<button disabled={authBusy} onClick={() => { setRegistration(!registration); setAuthError(''); }}>{registration ? '登录已有账号' : '注册普通账号'}</button></p>
    </div><button className="login-status" onClick={() => void openStatus()}><span className={`status-dot ${health?.status === 'ready' && !healthError ? 'healthy' : ''}`} />{statusLabel}<ChevronRight size={14} /></button></section>
    {statusPanel}
  </div>;

  return <div className="workspace-layout">
    {sidebarOpen && <button className="nav-scrim" aria-label="收起导航" onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? 'sidebar-open' : ''}`} aria-label="主导航"><Brand />
      <button className="new-chat" onClick={newConversation}><Plus size={18} />新建对话</button>
      <div className="sidebar-body"><nav><button className={page === 'conversation' ? 'nav-item active' : 'nav-item'} aria-current={page === 'conversation' ? 'page' : undefined} onClick={() => selectPage('conversation')}><MessageSquare size={18} />对话工作台</button>
        <button className={page === 'knowledge' ? 'nav-item active' : 'nav-item'} aria-current={page === 'knowledge' ? 'page' : undefined} onClick={() => selectPage('knowledge')}><BookOpen size={18} />知识库管理</button>
        <button className={page === 'automation' ? 'nav-item active' : 'nav-item'} aria-current={page === 'automation' ? 'page' : undefined} onClick={() => selectPage('automation')}><Workflow size={18} />自动化任务流</button>
        {user.role === 'admin' && <><button className={page === 'staff' ? 'nav-item active' : 'nav-item'} onClick={() => selectPage('staff')}><UsersRound size={18} />职员管理</button>
          <button className={page === 'audit' ? 'nav-item active' : 'nav-item'} onClick={() => selectPage('audit')}><ShieldCheck size={18} />调试与审计</button></>}</nav>
      <div className="history-section"><div className="section-label">历史会话<button aria-label="刷新历史会话" onClick={()=>void refreshHistory()}><RefreshCw size={16} /></button></div>
        <label className="history-search"><Search size={16} aria-hidden="true" /><input aria-label="搜索历史会话" placeholder="搜索会话标题" value={historyQuery} onChange={event=>setHistoryQuery(event.target.value)} /></label>
        {historyError && <p className="error-text">{historyError}</p>}{historyRows.length===0 && !historyError && <p>发送第一条消息后，会话会保存在这里。</p>}
        <div className="history-items">{historyRows.filter(row=>row.title.toLowerCase().includes(historyQuery.toLowerCase())).map(row=><button key={row.conversation_id} className={conversationId===row.conversation_id ? 'history-item active' : 'history-item'} onClick={()=>openConversation(row.conversation_id)}><span>{row.title}</span><time dateTime={row.updated_at}>{beijingTime(row.updated_at)}</time></button>)}</div>
      </div></div>
      <div className="sidebar-footer"><button className="service-status" onClick={() => void openStatus()}><span className={`status-dot ${health?.status === 'ready' && !healthError ? 'healthy' : ''}`} />{statusLabel}<ChevronRight size={14} /></button>
        <button className="profile-button" onClick={() => selectPage('profile')}><span className="avatar"><UserRound size={20} /></span><span><strong>{user.display_name}</strong><small>{user.role === 'admin' ? '系统管理员' : user.department_name || '待分配部门'}</small></span><ChevronRight size={15} /></button></div>
    </aside>
    <div className="main-area"><header className="topbar"><div><button className="mobile-menu" aria-label="展开导航" aria-expanded={sidebarOpen} onClick={() => setSidebarOpen(!sidebarOpen)}><Menu size={20} /></button><span>工作空间</span><ChevronRight size={14} /><strong>{pages[page]}</strong></div>
      <div><button className="header-login" onClick={() => void refreshUser()}>刷新资料</button><button className="header-login" onClick={() => void logout().catch(() => {})}>退出登录<ArrowRight size={15} /></button></div></header>
      <main className="workspace-content">
        {page === 'audit' && user.role === 'admin' ? <AuditPage key={user.user_id}/> : page === 'profile' ? <PersonalCenter user={user} refresh={refreshUser} logout={logout} /> : page === 'staff' && user.role === 'admin' ? <StaffManagement refresh={refreshUser} /> : page === 'knowledge' ? <KnowledgePage key={user.user_id} user={user} /> : page === 'conversation' ? <ChatWorkspace key={user.user_id} conversationId={conversationId} revision={chatRevision} userName={user.display_name} select={openConversation} changed={refreshHistory} />
          : <section className="module-placeholder"><span className="welcome-symbol">{page === 'automation' ? <Workflow size={26} /> : <UserRound size={26} />}</span><h1>{pages[page]}</h1>
            <p>{page === 'automation' ? '自动化任务流将在后续版本开放。' : '当前账号无法访问这个页面，请刷新资料。'}</p>
            <span className="placeholder-tag"><Check size={14} />入口已预留，功能尚未开放</span></section>}
      </main><footer className="workspace-footer"><span>{authError || '支持文字、表格与文档处理；业务数据库查询需确认，重要结论请核实。'}</span><button onClick={() => void openStatus()}><Activity size={18} />运行状态</button></footer>
    </div>{statusPanel}
  </div>;
}
