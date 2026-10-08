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
