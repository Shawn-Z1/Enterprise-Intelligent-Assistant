-- M04 会话排序、幂等发送、后台领取和终止状态；不重写现有 Demo 或原始审计。
ALTER TABLE app_runtime.messages ADD COLUMN message_order bigint GENERATED ALWAYS AS IDENTITY;
ALTER TABLE app_runtime.messages ADD COLUMN run_id varchar(16) REFERENCES app_runtime.runs;
CREATE UNIQUE INDEX messages_order ON app_runtime.messages(message_order);
CREATE UNIQUE INDEX one_assistant_message_per_run ON app_runtime.messages(run_id) WHERE role='assistant';
ALTER TABLE app_runtime.runs ADD COLUMN request_key varchar(16);
ALTER TABLE app_runtime.runs ADD COLUMN cancel_requested boolean NOT NULL DEFAULT false;
ALTER TABLE app_runtime.runs ADD CONSTRAINT run_request_unique UNIQUE(user_id,request_key);
CREATE UNIQUE INDEX one_active_run_per_conversation ON app_runtime.runs(conversation_id)
    WHERE status IN ('pending','running');
CREATE INDEX run_claim_queue ON app_runtime.runs(created_at) WHERE status='pending';
CREATE INDEX run_replay ON app_runtime.run_events(run_id,sequence);
