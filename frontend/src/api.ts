export interface DependencyStatus {
  status: 'ready' | 'unavailable';
  target: string;
  latency_ms: number;
  detail: string;
}

export interface Readiness {
  status: 'ready' | 'not_ready';
  dependencies: Record<string, DependencyStatus>;
  trace_id: string;
}

export interface RunEvent {
  event_id: number;
  trace_id: string;
  run_id: string | null;
  timestamp: string;
  type: string;
  stage: string;
  data: Record<string, unknown>;
}

export interface UserProfile {
  user_id: string; username: string; display_name: string; role: 'member' | 'admin';
  department_id: string | null; department_name: string | null; grade: number | null;
  position: string | null; employee_id: string | null; knowledge_enabled: boolean;
  business_query_enabled: boolean; permission_version: number;
  can_use_knowledge: boolean; can_query_business: boolean;
}

export interface Conversation { conversation_id: string; title: string; created_at: string; updated_at: string; }
export interface ChatRun {
  run_id: string; conversation_id: string; trace_id: string; status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
  created_at: string; updated_at: string; error?: {code: string; message: string} | null;
}
export interface ChatMessage {
  message_id: string; role: string; content: string; created_at: string; run_id: string | null; run_status: ChatRun['status'] | null;
  error?: {code: string; message: string} | null; model?: string | null;
  usage?: {source: 'reported' | 'estimated' | 'unavailable'; total_tokens: number | null} | null; finish_reason?: string | null;
}
export interface ConversationDetail { conversation_id: string; title: string; messages: ChatMessage[]; active_run: ChatRun | null; last_run: ChatRun | null; }
export interface ChatEvent { sequence: number; run_id: string; trace_id: string; timestamp: string; type: string; data: {text?: string; message?: string; model?: string; slot?: string; attempt?: number; next_attempt?: number; status?: ChatRun['status']; error?: ChatRun['error']}; }

/** 接收带时区日期，返回固定北京时间文本，避免浏览器所在国家改变界面时间。 */
export function beijingTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value));
}

/** 无参数生成 16 字符随机发送标识；用于重传幂等，不充当认证凭证。 */
export function requestId(): string {
  return btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(12)))).replaceAll('+','-').replaceAll('/','_');
}

export class ApiError extends Error {
  /** 接收 HTTP 状态、错误码及中文说明；创建可区分未登录与权限失败的错误。 */
  constructor(public status: number, public code: string, message: string) { super(message); }
}

/** 接收路径、HTTP 方法及可选 JSON；同源发送会话 Cookie，写操作带会话绑定的 CSRF。 */
export async function apiRequest<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const csrf = document.cookie.split('; ').find(item => item.startsWith('zhixing_csrf='))?.split('=').slice(1).join('=') || '';
  const response = await fetch(`/api${path}`, { method, credentials: 'same-origin', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...(method !== 'GET' ? {'X-CSRF-Token': decodeURIComponent(csrf)} : {}) },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: { code: 'NETWORK_ERROR', message: '服务暂时不可用，请稍后重试' } })) as { error: { code: string; message: string } };
    throw new ApiError(response.status, error.error.code, error.error.message);
  }
  return response.json() as Promise<T>;
}

/** 读取真实依赖状态；参数 signal 可取消请求，返回含逐项探针的就绪响应。 */
export async function fetchReadiness(signal?: AbortSignal): Promise<Readiness> {
  const response = await fetch('/api/health/ready', { signal, cache: 'no-store' });
  if (response.status !== 200 && response.status !== 503) throw new Error('无法连接后端服务');
  return response.json() as Promise<Readiness>;
}

/** 读取 M01 基础设施事件；无参数，返回当前进程的事件窗口，不是永久审计。 */
export async function fetchEvents(): Promise<RunEvent[]> {
  const response = await fetch('/api/events');
  if (!response.ok) throw new Error('无法读取基础设施事件');
  const result = await response.json() as { events: RunEvent[] };
  return result.events;
}
