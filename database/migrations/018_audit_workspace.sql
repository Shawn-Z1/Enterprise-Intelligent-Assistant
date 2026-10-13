-- 只读审计列表筛选与关联索引；原始事件、模型请求及用户消息不修改。
CREATE INDEX audit_trace_created ON app_audit.traces(created_at DESC,trace_id);
CREATE INDEX audit_events_kind ON app_audit.events(trace_id,event_type,event_id);
CREATE INDEX audit_attempt_trace ON app_runtime.model_attempts(trace_id,created_at);
CREATE INDEX audit_runs_trace ON app_runtime.runs(trace_id);
CREATE INDEX audit_memory_trace ON app_memory.jobs(trace_id);
CREATE INDEX audit_knowledge_trace ON app_knowledge.jobs(trace_id);
