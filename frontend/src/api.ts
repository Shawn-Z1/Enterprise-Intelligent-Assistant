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
  run_id: string; conversation_id: string; trace_id: string; status: 'pending' | 'running' | 'waiting_confirmation' | 'waiting_sandbox' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
  pending_action?: string | null;
  created_at: string; updated_at: string; error?: {code: string; message: string} | null;
}
export interface ChatMessage {
  message_id: string; role: string; content: string; created_at: string; run_id: string | null; run_status: ChatRun['status'] | null;
  error?: {code: string; message: string} | null; model?: string | null;
  usage?: {source: 'reported' | 'estimated' | 'unavailable'; total_tokens: number | null} | null; finish_reason?: string | null;
  citations?: Citation[];
  attachments?: AnalysisAttachment[];
}
export interface Citation { type?: 'knowledge' | 'web' | 'business' | 'sandbox' | 'memory'; result_id?: string; label: string; chunk_id?: string; document_id?: string; filename?: string; page?: number | null; heading?: string | null; position?: number;
  remembered_at?: string; topic_id?: string;
  source_id?: string; title?: string; url?: string; website?: string; published_at?: string | null; fetched_at?: string;
  content_kind?: 'search_snippet' | 'provider_excerpt' | 'page_text'; page_read?: boolean; truncated?: boolean;
  page_numbers?: number[] | null; heading_path?: {level:number;title:string}[] | null; }

/** 接收引用真实页集合，返回单页、连续页范围或非连续页列表，兼容旧引用而不虚报跨页位置。 */
export function citationPages(source: Citation): string {
  const pages=source.page_numbers?.length ? source.page_numbers : source.page ? [source.page] : [];
  if(!pages.length)return '';
  const contiguous=pages.every((page,index)=>index===0 || page===pages[index-1]+1);
  return `第 ${pages.length>1 && contiguous ? `${pages[0]}–${pages[pages.length-1]}` : pages.join('、')} 页`;
}

/** 接收内部或公开引用，返回可读来源标签；不将网页标题冒充内部文件名。 */
export function citationTitle(source: Citation): string {
  return source.type==='memory' ? `个人记忆 · ${source.title || '项目'}` : source.type==='sandbox' ? `文件 · ${source.title || '分析结果'}` : source.type==='business' ? '业务 · 查询结果' : source.type==='web' ? `公开 · ${source.title || source.website || '网页'}` : `内部 · ${source.filename || '知识文档'}`;
}
export interface ConversationDetail { conversation_id: string; title: string; messages: ChatMessage[]; active_run: ChatRun | null; last_run: ChatRun | null; }
export interface ChatEvent { sequence: number; run_id: string; trace_id: string; timestamp: string; type: string; data: {text?: string; message?: string; model?: string; slot?: string; attempt?: number; next_attempt?: number; status?: ChatRun['status']; error?: ChatRun['error']; action?: SensitiveAction}; }

export interface SensitiveAction { action_id:string; run_id:string; call_id:string; action_type:string; status:string; expires_at:string; request:{purpose:string; target:Record<string,unknown>; payload:Record<string,unknown>}; }
export interface BusinessResultData { result_id:string; action_id:string; columns:string[]; rows:Record<string,unknown>[]; row_count:number; truncated:boolean; scope:string; chart:{type:'bar'|'line';title:string;unit:string;time_range:string;partial:boolean;points:{label:string;value:number}[]} | null; }

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

/** 接收知识上传表单，同源发送原件与 CSRF，返回逐文件结果，不手动覆盖 multipart 边界。 */
export async function uploadKnowledge<T>(form: FormData): Promise<T> {
  const csrf = document.cookie.split('; ').find(item => item.startsWith('zhixing_csrf='))?.split('=').slice(1).join('=') || '';
  const response = await fetch('/api/knowledge/applications',{method:'POST',credentials:'same-origin',
    headers:{'X-CSRF-Token':decodeURIComponent(csrf)},body:form});
  if (!response.ok) {
    const value = await response.json();
    throw new ApiError(response.status,value.error.code,value.error.message);
  }
  return response.json() as Promise<T>;
}

/** 接收已鉴权原件或 PNG 路径及取消信号，返回 Blob；错误只显示服务器安全说明。 */
export async function knowledgeBlob(path: string,signal?: AbortSignal): Promise<Blob> {
  const response = await fetch(`/api${path}`,{credentials:'same-origin',cache:'no-store',signal});
  if (!response.ok) {
    const value = await response.json();
    throw new ApiError(response.status,value.error.code,value.error.message);
  }
  return response.blob();
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

export interface AnalysisAttachment {attachment_id:string;filename:string;format:string;size_bytes:number;original_sha256?:string;message_id?:string|null;}
export interface AttachmentSummary {attachment_id:string;file:string;format?:string;status?:string;error?:string;sheet?:string;table?:string;columns?:string[];row_count?:number;missing?:Record<string,number>;sample?:unknown;sample_complete?:boolean;text_length?:number;page_count?:number;headings?:string[];empty_pages?:number[];}
export interface SandboxResultData {result_id:string;operation?:string;description?:string;message?:string;text?:string;stdout?:string;coverage?:{scope:string;unit:string;start:number;end:number;total:number;complete:boolean};has_more?:boolean;next_offset?:number;columns?:string[];rows?:Record<string,unknown>[];row_count?:number;truncated?:boolean;chart?:{title?:string;partial?:boolean};files?:AttachmentSummary[];artifacts:{artifact_id:string;filename:string;size_bytes:number;sha256:string}[];}
/** 接收本人会话编号及文件表单，发送原件和会话CSRF，不覆盖浏览器multipart边界。 */
export async function uploadAnalysis(conversationId:string,form:FormData):Promise<AnalysisAttachment[]>{
  const csrf=document.cookie.split('; ').find(item=>item.startsWith('zhixing_csrf='))?.split('=').slice(1).join('=') || '';
  const response=await fetch(`/api/conversations/${conversationId}/attachments`,{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':decodeURIComponent(csrf)},body:form});
  const value=await response.json();if(!response.ok)throw new Error(value.error?.message || '上传失败');return value;
}
