-- 摘要只追加版本，原消息/工具/确认/图检查点保持独立真源。
ALTER TABLE app_runtime.context_summaries ADD COLUMN user_id varchar(16) REFERENCES app_system.users;
ALTER TABLE app_runtime.context_summaries ADD COLUMN permission_version bigint;
ALTER TABLE app_runtime.context_summaries ADD COLUMN source_hash text;
ALTER TABLE app_runtime.context_summaries ADD COLUMN metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb;
CREATE UNIQUE INDEX context_summary_source ON app_runtime.context_summaries(user_id,conversation_id,permission_version,source_hash);
