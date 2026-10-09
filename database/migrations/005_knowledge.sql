-- M05 原件、审核快照和解析记录；不写正式切片或向量索引，不修改既有数据。
ALTER TABLE app_knowledge.applications ADD COLUMN filename text;
ALTER TABLE app_knowledge.applications ADD COLUMN file_format text;
ALTER TABLE app_knowledge.applications ADD COLUMN byte_size bigint;
ALTER TABLE app_knowledge.applications ADD COLUMN fingerprint text;
ALTER TABLE app_knowledge.applications ADD COLUMN approval_origin text;
ALTER TABLE app_knowledge.applications ADD COLUMN parse_status text NOT NULL DEFAULT 'pending';
ALTER TABLE app_knowledge.applications ADD COLUMN error_json jsonb;
ALTER TABLE app_knowledge.applications ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE app_knowledge.reviews ADD COLUMN comment text;
ALTER TABLE app_knowledge.documents ADD COLUMN approved_fingerprint text;
CREATE UNIQUE INDEX knowledge_document_application ON app_knowledge.documents(application_id);
ALTER TABLE app_knowledge.jobs ADD COLUMN job_type text NOT NULL DEFAULT 'publish';
ALTER TABLE app_knowledge.jobs ADD COLUMN expected_fingerprint text;
ALTER TABLE app_knowledge.jobs ADD COLUMN trace_id varchar(16);
ALTER TABLE app_knowledge.jobs ADD COLUMN error_json jsonb;
ALTER TABLE app_knowledge.jobs ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX knowledge_job_queue ON app_knowledge.jobs(created_at) WHERE status='pending';
CREATE UNIQUE INDEX knowledge_active_job ON app_knowledge.jobs(application_id,job_type) WHERE status IN ('pending','running');
CREATE TABLE app_knowledge.parses (
    parse_id varchar(16) PRIMARY KEY,
    application_id varchar(16) NOT NULL REFERENCES app_knowledge.applications,
    fingerprint text NOT NULL,
    source_sha256 text NOT NULL,
    content_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(application_id,fingerprint)
);
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON app_knowledge.parses
    FOR EACH ROW EXECUTE FUNCTION app_audit.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON app_knowledge.parses
    FOR EACH STATEMENT EXECUTE FUNCTION app_audit.reject_mutation();
