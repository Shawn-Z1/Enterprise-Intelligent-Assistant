-- M06 正式切片、索引版本与不可变检索证据；旧原文与 Demo 业务数据不改写。
ALTER TABLE app_knowledge.chunks ADD COLUMN status text NOT NULL DEFAULT 'active';
ALTER TABLE app_knowledge.chunks ADD COLUMN source_sha256 text;
ALTER TABLE app_knowledge.chunks ADD COLUMN identifiers varchar(64)[] NOT NULL DEFAULT '{}';
ALTER TABLE app_knowledge.chunks ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE app_knowledge.documents ADD COLUMN collection_name text;
ALTER TABLE app_knowledge.jobs ADD COLUMN index_space text;
CREATE TABLE app_runtime.retrievals (
    retrieval_id varchar(16) PRIMARY KEY,
    trace_id varchar(16) NOT NULL,
    run_id varchar(16) REFERENCES app_runtime.runs,
    user_id varchar(16) NOT NULL REFERENCES app_system.users,
    permission_version bigint NOT NULL,
    query text NOT NULL,
    evidence_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX retrievals_run ON app_runtime.retrievals(run_id,created_at);
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON app_runtime.retrievals
    FOR EACH ROW EXECUTE FUNCTION app_audit.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON app_runtime.retrievals
    FOR EACH STATEMENT EXECUTE FUNCTION app_audit.reject_mutation();
ALTER TABLE app_runtime.tool_calls ADD COLUMN provider_call_id text;
ALTER TABLE app_runtime.tool_calls ADD COLUMN protocol_message jsonb;
