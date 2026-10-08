import { useCallback, useEffect, useState } from 'react';
import { Activity, ArrowRight, ArrowUp, BookOpen, Check, ChevronRight, CircleHelp,
  Eye, EyeOff, FileText, Menu, MessageSquare, Plus, RefreshCw, Search,
  ShieldCheck, UserRound, Workflow, X } from 'lucide-react';
import { fetchEvents, fetchReadiness, type Readiness, type RunEvent } from './api';

type Page = 'conversation' | 'knowledge' | 'automation' | 'profile';
const pages = { conversation: '新建对话', knowledge: '知识库管理', automation: '自动化任务流', profile: '个人中心' };

/** 无参数渲染文字品牌标识；返回中文标记与产品名，不包含交互。 */
function Brand() {
  return <div className="brand"><span className="brand-mark" aria-hidden="true">知</span>
    <span>知行<span className="brand-caption">企业智能助理</span></span></div>;
}

/** 无参数渲染 M01 页面壳及真实健康状态；返回根界面，不创建认证或模型任务。 */
export default function App() {
  // 页面壳与认证状态分开；预览工作台绝不代表已登录。
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
    function restoreView() { setView(location.pathname === '/login' ? 'login' : 'workspace'); }
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
      <div><strong>{name === 'postgres' ? 'PostgreSQL' : name === 'minio' ? 'MinIO' : name === 'redis' ? 'Redis' : 'Milvus'}</strong>
        <span className={dependency.status === 'ready' ? 'healthy-text' : 'error-text'}>{dependency.status === 'ready' ? '已连通' : '不可用'}</span></div>
      <code>{dependency.target}</code><p>{dependency.detail}</p><small>{dependency.latency_ms} ms</small>
    </div>)}
    <h3>基础事件</h3><p className="muted small">M01 仅当前进程最近 100 条基础设施事件。永久审计将在 M02 建立。</p>
    {eventsError && <p className="error-text">{eventsError}</p>}
    <div className="event-list">{events.slice(-6).reverse().map(event => <div key={event.event_id}>
      <span>#{event.event_id} · {new Date(event.timestamp).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai' })}</span>
      <strong>{event.type}</strong><code title={event.trace_id}>{event.trace_id}</code>
    </div>)}</div>
  </aside>;

  if (view === 'login') return <div className="login-layout">
    <section className="login-story"><Brand />
      <div className="login-intro"><h1>企业知识与业务，<br />在一个工作台处理。</h1><p>查询企业资料、分析业务数据、整理会议记录。<br />使用企业账号，进入你的工作空间。</p>
      <div className="story-features">
        <div><BookOpen size={24} aria-hidden="true" /><span><strong>企业知识</strong><p>制度、流程与项目资料，回答附有来源。</p></span></div>
        <div><Activity size={24} aria-hidden="true" /><span><strong>业务数据</strong><p>在权限范围内查询，执行前由本人确认。</p></span></div>
        <div><FileText size={24} aria-hidden="true" /><span><strong>记录与沉淀</strong><p>整理对话和会议文字，提交后审核入库。</p></span></div>
      </div></div>
      <div className="story-note"><ShieldCheck size={20} aria-hidden="true" /><span>可信身份、受控访问、操作可追溯</span></div>
    </section>
    <section className="login-form-area"><div className="login-form-wrap"><span className="stage-badge">M01 · 工程骨架</span>
      <h2>{registration ? '创建账号' : '欢迎来到知行'}</h2><p className="muted">{registration ? '注册后由管理员分配业务权限' : '使用企业账号，开启你的工作空间'}</p>
      <form onSubmit={event => event.preventDefault()}>
        <label htmlFor="account">账号</label><input id="account" autoComplete="username" placeholder="请输入账号" />
        <label htmlFor="password">密码</label><div className="password-input"><input id="password" type={passwordVisible ? 'text' : 'password'} autoComplete={registration ? 'new-password' : 'current-password'} placeholder="请输入密码" />
          <button type="button" aria-label={passwordVisible ? '隐藏密码' : '显示密码'} onClick={() => setPasswordVisible(!passwordVisible)}>{passwordVisible ? <EyeOff size={18} /> : <Eye size={18} />}</button></div>
        <p className="form-notice" id="auth-notice">账号认证将在 M03 开放，当前页面仅供界面预览。</p>
        <button className="primary-button" disabled aria-describedby="auth-notice">{registration ? '注册暂未开放' : '登录暂未开放'}</button>
      </form>
      <p className="switch-form">{registration ? '已有账号？' : '还没有账号？'}<button onClick={() => setRegistration(!registration)}>{registration ? '查看登录页面' : '查看注册页面'}</button></p>
      <button className="preview-link" onClick={() => navigate('workspace')}>预览工作台<ArrowRight size={16} /></button>
    </div><button className="login-status" onClick={() => void openStatus()}><span className={`status-dot ${health?.status === 'ready' && !healthError ? 'healthy' : ''}`} />{statusLabel}<ChevronRight size={14} /></button></section>
    {statusPanel}
  </div>;

  return <div className="workspace-layout">
    {sidebarOpen && <button className="nav-scrim" aria-label="收起导航" onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? 'sidebar-open' : ''}`} aria-label="主导航"><Brand />
      <button className="new-chat" onClick={() => selectPage('conversation')}><Plus size={18} />新建对话<span>预览</span></button>
      <nav><button className={page === 'conversation' ? 'nav-item active' : 'nav-item'} aria-current={page === 'conversation' ? 'page' : undefined} onClick={() => selectPage('conversation')}><MessageSquare size={18} />对话工作台</button>
        <button className={page === 'knowledge' ? 'nav-item active' : 'nav-item'} aria-current={page === 'knowledge' ? 'page' : undefined} onClick={() => selectPage('knowledge')}><BookOpen size={18} />知识库管理</button>
        <button className={page === 'automation' ? 'nav-item active' : 'nav-item'} aria-current={page === 'automation' ? 'page' : undefined} onClick={() => selectPage('automation')}><Workflow size={18} />自动化任务流</button></nav>
      <div className="history-section"><div className="section-label">历史会话<button aria-label="搜索历史会话（M04 开放）" disabled><Search size={15} /></button></div><p>还没有会话记录</p><small>真实会话将在 M04 开放</small></div>
      <div className="sidebar-footer"><button className="service-status" onClick={() => void openStatus()}><span className={`status-dot ${health?.status === 'ready' && !healthError ? 'healthy' : ''}`} />{statusLabel}<ChevronRight size={14} /></button>
        <button className="profile-button" onClick={() => selectPage('profile')}><span className="avatar"><UserRound size={20} /></span><span><strong>访客预览</strong><small>尚未登录</small></span><ChevronRight size={15} /></button></div>
    </aside>
    <div className="main-area"><header className="topbar"><div><button className="mobile-menu" aria-label="展开导航" aria-expanded={sidebarOpen} onClick={() => setSidebarOpen(!sidebarOpen)}><Menu size={20} /></button><span>工作空间</span><ChevronRight size={14} /><strong>{pages[page]}</strong></div>
      <div><span className="stage-badge">M01 · 预览</span><button className="header-login" onClick={() => navigate('login')}>账号登录<ArrowRight size={15} /></button></div></header>
      <main className="workspace-content">
        {page === 'conversation' ? <div className="conversation-shell"><div className="conversation-overview"><div className="welcome"><h1>新建对话</h1><p>在这里查询资料、分析数据，或整理工作记录。</p></div>
          <section className="task-guide" aria-label="工作台功能预览"><div className="guide-heading"><h2>工作台可以处理什么</h2><span>功能按开发阶段开放</span></div>
            <div className="guide-row"><BookOpen size={24} aria-hidden="true" /><div><h3>查询企业知识</h3><p>查找制度、流程和项目资料，查看回答引用的原文。</p></div><span>知识检索将在 M06 开放</span></div>
            <div className="guide-row"><Activity size={24} aria-hidden="true" /><div><h3>分析业务数据</h3><p>按实际权限查询业务记录，确认执行后查看数据与图表。</p></div><span>业务查询将在 M07 开放</span></div>
            <div className="guide-row"><FileText size={24} aria-hidden="true" /><div><h3>整理会议与对话</h3><p>把工作文字整理为纪要或知识文稿，保留未确定事项。</p></div><span>文字整理将在 M08–M09 开放</span></div>
          </section></div>
          <div className="composer-area"><div className="stage-note"><CircleHelp size={15} /><span>当前为工作台预览。模型会话将在 M04 接入。</span></div><div className="composer"><label className="sr-only" htmlFor="message">对话输入（暂未开放）</label><textarea id="message" disabled placeholder="向知行提问，或描述你需要完成的工作…" /><div><span><Plus size={18} />附件暂未开放</span><button disabled aria-label="发送消息（M04 开放）"><ArrowUp size={20} /></button></div></div><p className="composer-footer">回答将基于你的实际权限与可用证据。重要结论请核实。</p></div></div>
          : <section className="module-placeholder"><span className="welcome-symbol">{page === 'knowledge' ? <BookOpen size={26} /> : page === 'automation' ? <Workflow size={26} /> : <UserRound size={26} />}</span><h1>{pages[page]}</h1>
            <p>{page === 'knowledge' ? '知识提交、审核与文件预览将在 M05 开放。' : page === 'automation' ? '自动化任务流将在后续版本开放。' : '账号登录和本人资料将在 M03 开放。'}</p>
            <span className="placeholder-tag"><Check size={14} />入口已预留 · 功能尚未开放</span></section>}
      </main><footer className="workspace-footer"><span>工作台预览，业务功能将按阶段开放</span><button onClick={() => void openStatus()}><Activity size={18} />运行状态</button></footer>
    </div>{statusPanel}
  </div>;
}
