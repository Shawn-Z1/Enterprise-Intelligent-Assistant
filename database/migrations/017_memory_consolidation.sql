-- 语义整理回执永久追加；重复确认的水位独立于可读快照，旧作业不能复活已合并key。
ALTER TABLE app_memory.state ADD COLUMN personal_watermarks jsonb NOT NULL DEFAULT '{}';
CREATE TABLE app_memory.consolidations (
    consolidation_id varchar(16) PRIMARY KEY,
    job_id varchar(16) NOT NULL REFERENCES app_memory.jobs,
    extraction_id varchar(16) NOT NULL REFERENCES app_memory.extractions,
    base_snapshot_id varchar(16) REFERENCES app_memory.snapshots,
    result_snapshot_id varchar(16) REFERENCES app_memory.snapshots,
    status text NOT NULL CHECK(status IN ('applied','no_change','version_conflict')),
    operations_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX memory_consolidation_finished ON app_memory.consolidations(job_id) WHERE status IN ('applied','no_change');
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON app_memory.consolidations FOR EACH ROW EXECUTE FUNCTION app_audit.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON app_memory.consolidations FOR EACH STATEMENT EXECUTE FUNCTION app_audit.reject_mutation();
