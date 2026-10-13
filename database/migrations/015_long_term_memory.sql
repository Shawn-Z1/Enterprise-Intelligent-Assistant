-- M11增量队列及有效快照索引；旧提取/快照始终追加，不修改历史迁移。
ALTER TABLE app_memory.jobs ADD COLUMN run_id varchar(16) REFERENCES app_runtime.runs;
ALTER TABLE app_memory.jobs ADD COLUMN trace_id varchar(16);
ALTER TABLE app_memory.jobs ADD COLUMN snapshot_id varchar(16) REFERENCES app_memory.snapshots;
ALTER TABLE app_memory.jobs ADD COLUMN attempts integer NOT NULL DEFAULT 0;
ALTER TABLE app_memory.jobs ADD COLUMN available_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE app_memory.jobs ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE app_memory.jobs ADD COLUMN error_json jsonb;
CREATE UNIQUE INDEX memory_extract_run ON app_memory.jobs(run_id) WHERE job_type='extract';
CREATE UNIQUE INDEX memory_index_snapshot ON app_memory.jobs(snapshot_id) WHERE job_type='index';
CREATE INDEX memory_pending_jobs ON app_memory.jobs(available_at,created_at) WHERE status='pending';
CREATE UNIQUE INDEX memory_extraction_watermark ON app_memory.extractions(user_id,conversation_id,message_watermark);
ALTER TABLE app_memory.topics ADD COLUMN topic_key varchar(16);
ALTER TABLE app_memory.topics ADD COLUMN aliases_json jsonb NOT NULL DEFAULT '[]';
ALTER TABLE app_memory.topics ADD COLUMN summary text NOT NULL DEFAULT '';
ALTER TABLE app_memory.topics ADD COLUMN source_json jsonb NOT NULL DEFAULT '{}';
CREATE UNIQUE INDEX memory_snapshot_topic ON app_memory.topics(snapshot_id,topic_key);
CREATE TABLE app_memory.chunks (
    chunk_id varchar(16) PRIMARY KEY,
    topic_id varchar(16) NOT NULL REFERENCES app_memory.topics,
    snapshot_id varchar(16) NOT NULL REFERENCES app_memory.snapshots,
    user_id varchar(16) NOT NULL REFERENCES app_system.users,
    position integer NOT NULL,
    content text NOT NULL,
    embedding_space text NOT NULL,
    index_version integer NOT NULL,
    UNIQUE(topic_id,position,embedding_space,index_version)
);
ALTER TABLE app_memory.state ADD COLUMN indexed_snapshot_id varchar(16) REFERENCES app_memory.snapshots;
ALTER TABLE app_memory.state ADD COLUMN embedding_space text;
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON app_memory.chunks FOR EACH ROW EXECUTE FUNCTION app_audit.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON app_memory.chunks FOR EACH STATEMENT EXECUTE FUNCTION app_audit.reject_mutation();
