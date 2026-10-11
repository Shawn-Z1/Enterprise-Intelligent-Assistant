-- M07B 保留历史批准/结果，新增通用附件与独立持久队列，不改已应用迁移。
ALTER TABLE app_runtime.analysis_attachments DROP CONSTRAINT analysis_attachments_format_check;
ALTER TABLE app_runtime.analysis_attachments ADD CONSTRAINT analysis_attachments_format_check
 CHECK(format IN ('csv','xlsx','txt','md','docx','pdf'));
DROP INDEX app_runtime.one_active_run_per_conversation;
CREATE UNIQUE INDEX one_active_run_per_conversation ON app_runtime.runs(conversation_id)
 WHERE status IN ('pending','running','waiting_confirmation','waiting_sandbox');

CREATE TABLE app_runtime.sandbox_jobs (
 job_id varchar(16) PRIMARY KEY,run_id varchar(16) NOT NULL REFERENCES app_runtime.runs,
 user_id varchar(16) NOT NULL REFERENCES app_system.users,call_id varchar(16) UNIQUE REFERENCES app_runtime.tool_calls,
 idempotency_key text NOT NULL UNIQUE,operation text NOT NULL CHECK(operation IN ('prepare','read','execute')),
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','completed','failed','cancelled','unknown')),
 input_json jsonb NOT NULL,node_policy jsonb NOT NULL,request_fingerprint text NOT NULL,
 runner_id text,lease_version integer NOT NULL DEFAULT 0,lease_until timestamptz,
 cancel_requested boolean NOT NULL DEFAULT false,result_json jsonb,
 expires_at timestamptz NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
 started_at timestamptz,finished_at timestamptz,updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sandbox_job_queue ON app_runtime.sandbox_jobs(created_at) WHERE status='queued';
CREATE INDEX sandbox_job_run ON app_runtime.sandbox_jobs(run_id);
ALTER TABLE app_runtime.sandbox_results ALTER COLUMN attempt_id DROP NOT NULL;
ALTER TABLE app_runtime.sandbox_results ADD COLUMN job_id varchar(16) UNIQUE REFERENCES app_runtime.sandbox_jobs;
