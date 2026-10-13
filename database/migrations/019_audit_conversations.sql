-- 会话视图和逐轮读取使用现有永久记录，仅追加查询索引，不改变消息/审计归属。
CREATE INDEX audit_conversation_messages ON app_runtime.messages(conversation_id,message_order);
CREATE INDEX audit_conversation_runs ON app_runtime.runs(conversation_id,created_at,run_id);
