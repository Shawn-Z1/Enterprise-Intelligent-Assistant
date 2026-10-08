-- M02 唯一结构来源：系统、运行、知识、记忆、审计与模拟业务按 schema 隔离。
CREATE SCHEMA app_system;
CREATE SCHEMA app_runtime;
CREATE SCHEMA app_knowledge;
CREATE SCHEMA app_memory;
CREATE SCHEMA app_audit;
CREATE SCHEMA demo_business;

CREATE TABLE app_system.schema_migrations (
    version text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_system.seed_receipts (
    source_key text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_system.departments (
    department_id uuid PRIMARY KEY, code text NOT NULL UNIQUE, name text NOT NULL UNIQUE,
    enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE app_system.users (
    user_id uuid PRIMARY KEY, username text NOT NULL UNIQUE, display_name text NOT NULL,
    password_hash text NOT NULL CHECK (password_hash LIKE '$argon2id$%'),
    role text NOT NULL DEFAULT 'member' CHECK (role IN ('member', 'admin')),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    department_id uuid REFERENCES app_system.departments, grade smallint CHECK (grade BETWEEN 1 AND 10),
    employee_id uuid UNIQUE, knowledge_enabled boolean NOT NULL DEFAULT false,
    business_query_enabled boolean NOT NULL DEFAULT false, permission_version bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_system.auth_sessions (
    session_id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app_system.users,
    token_hash text NOT NULL UNIQUE, expires_at timestamptz NOT NULL, revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE demo_business.departments (
    department_id uuid PRIMARY KEY REFERENCES app_system.departments, code text NOT NULL UNIQUE,
    name text NOT NULL, manager_employee_id uuid, enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE demo_business.employees (
    employee_id uuid PRIMARY KEY, user_id uuid UNIQUE REFERENCES app_system.users,
    name text NOT NULL, department_id uuid NOT NULL REFERENCES demo_business.departments,
    position text NOT NULL, grade smallint NOT NULL CHECK (grade BETWEEN 1 AND 10),
    hire_date date NOT NULL, status text NOT NULL, work_email text NOT NULL, contact_phone text,
    UNIQUE (user_id, employee_id)
);
-- 使用可信 ID 建立双向身份关系；延迟外键允许在一个事务内导入账号和员工。
ALTER TABLE app_system.users ADD CONSTRAINT user_employee_matches
    FOREIGN KEY (user_id, employee_id) REFERENCES demo_business.employees(user_id, employee_id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE demo_business.departments ADD CONSTRAINT department_manager
    FOREIGN KEY (manager_employee_id) REFERENCES demo_business.employees
    DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE demo_business.attendance_records (
    attendance_id uuid PRIMARY KEY, employee_id uuid NOT NULL REFERENCES demo_business.employees,
    attendance_date date NOT NULL, check_in timestamptz, check_out timestamptz,
    status text NOT NULL, late_minutes integer NOT NULL DEFAULT 0 CHECK (late_minutes >= 0),
    absent_minutes integer NOT NULL DEFAULT 0 CHECK (absent_minutes >= 0),
    UNIQUE (employee_id, attendance_date)
);
CREATE TABLE demo_business.approval_records (
    approval_id uuid PRIMARY KEY, applicant_employee_id uuid NOT NULL REFERENCES demo_business.employees,
    handler_employee_id uuid REFERENCES demo_business.employees, approval_type text NOT NULL,
    title text NOT NULL, status text NOT NULL, created_at timestamptz NOT NULL, processed_at timestamptz
);
CREATE TABLE demo_business.tickets (
    ticket_id uuid PRIMARY KEY, submitter_employee_id uuid NOT NULL REFERENCES demo_business.employees,
    assignee_employee_id uuid REFERENCES demo_business.employees, category text NOT NULL,
    priority text NOT NULL, status text NOT NULL, created_at timestamptz NOT NULL,
    completed_at timestamptz, description text NOT NULL
);

CREATE TABLE app_runtime.conversations (
    conversation_id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app_system.users,
    title text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, user_id)
);
CREATE TABLE app_runtime.messages (
    message_id uuid PRIMARY KEY, conversation_id uuid NOT NULL, user_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('user','assistant','system','tool')),
    content text NOT NULL, protocol_json jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (conversation_id,user_id) REFERENCES app_runtime.conversations(conversation_id,user_id)
);
CREATE TABLE app_runtime.runs (
    run_id uuid PRIMARY KEY, trace_id uuid NOT NULL, conversation_id uuid NOT NULL, user_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'pending', state_json jsonb NOT NULL DEFAULT '{}',
    lease_owner text, lease_until timestamptz, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (conversation_id,user_id) REFERENCES app_runtime.conversations(conversation_id,user_id)
);
CREATE TABLE app_runtime.run_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, run_id uuid NOT NULL REFERENCES app_runtime.runs,
    trace_id uuid NOT NULL, sequence bigint NOT NULL CHECK (sequence > 0), event_type text NOT NULL,
    payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (run_id,sequence)
);
CREATE TABLE app_runtime.tool_calls (
    call_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES app_runtime.runs, tool_name text NOT NULL,
    input_json jsonb NOT NULL, output_json jsonb, status text NOT NULL, idempotency_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_runtime.query_approvals (
    approval_id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app_system.users,
    run_id uuid NOT NULL REFERENCES app_runtime.runs, call_id uuid NOT NULL REFERENCES app_runtime.tool_calls,
    query_fingerprint text NOT NULL, permission_version bigint NOT NULL, decision text NOT NULL DEFAULT 'pending',
    expires_at timestamptz NOT NULL, consumed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_runtime.checkpoints (
    checkpoint_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES app_runtime.runs,
    sequence bigint NOT NULL, state_json jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (run_id,sequence)
);
CREATE TABLE app_runtime.attachments (
    attachment_id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app_system.users,
    conversation_id uuid REFERENCES app_runtime.conversations, bucket text NOT NULL,
    object_key text NOT NULL, mime_type text NOT NULL, sha256 text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_runtime.context_summaries (
    summary_id uuid PRIMARY KEY, conversation_id uuid NOT NULL REFERENCES app_runtime.conversations,
    version bigint NOT NULL, covered_message_ids uuid[] NOT NULL, summary_text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (conversation_id,version)
);
CREATE TABLE app_runtime.model_attempts (
    attempt_id uuid PRIMARY KEY, trace_id uuid NOT NULL, span_id uuid NOT NULL,
    model_role text NOT NULL, provider text NOT NULL, model text NOT NULL, status text NOT NULL,
    request_json jsonb NOT NULL, response_json jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_runtime.query_results (
    result_id uuid PRIMARY KEY, call_id uuid NOT NULL REFERENCES app_runtime.tool_calls,
    result_json jsonb NOT NULL, chart_json jsonb, truncated boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_knowledge.applications (
    application_id uuid PRIMARY KEY, creator_user_id uuid NOT NULL REFERENCES app_system.users,
    title text NOT NULL, original_sha256 text NOT NULL, object_key text NOT NULL,
    allowed_department_ids uuid[] NOT NULL DEFAULT '{}', minimum_grade smallint CHECK (minimum_grade BETWEEN 1 AND 10),
    status text NOT NULL DEFAULT 'pending_review', acl_snapshot jsonb NOT NULL, source_ids jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_knowledge.reviews (
    review_id uuid PRIMARY KEY, application_id uuid NOT NULL REFERENCES app_knowledge.applications,
    reviewed_by uuid NOT NULL REFERENCES app_system.users, decision text NOT NULL CHECK (decision IN ('approved','rejected')),
    application_fingerprint text NOT NULL, acl_snapshot jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_knowledge.documents (
    document_id uuid PRIMARY KEY, application_id uuid NOT NULL REFERENCES app_knowledge.applications,
    creator_user_id uuid NOT NULL REFERENCES app_system.users, review_id uuid REFERENCES app_knowledge.reviews,
    status text NOT NULL, acl_snapshot jsonb NOT NULL, embedding_space text, index_version bigint,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_knowledge.chunks (
    chunk_id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES app_knowledge.documents,
    position integer NOT NULL, page_number integer, heading text, content text NOT NULL,
    acl_snapshot jsonb NOT NULL, embedding_space text NOT NULL, index_version bigint NOT NULL,
    UNIQUE (document_id,position,index_version)
);
CREATE TABLE app_knowledge.jobs (
    job_id uuid PRIMARY KEY, application_id uuid NOT NULL REFERENCES app_knowledge.applications,
    document_id uuid REFERENCES app_knowledge.documents, status text NOT NULL, attempts integer NOT NULL DEFAULT 0,
    lease_owner text, lease_until timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_memory.extractions (
    extraction_id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app_system.users,
    conversation_id uuid NOT NULL REFERENCES app_runtime.conversations, message_watermark uuid,
    raw_memory jsonb NOT NULL, source_message_ids uuid[] NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_memory.snapshots (
    snapshot_id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app_system.users, version bigint NOT NULL,
    profile_json jsonb NOT NULL, preferences_json jsonb NOT NULL, summary_md text NOT NULL,
    source_extraction_ids uuid[] NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (user_id,version)
);
CREATE TABLE app_memory.topics (
    topic_id uuid PRIMARY KEY, snapshot_id uuid NOT NULL REFERENCES app_memory.snapshots,
    user_id uuid NOT NULL REFERENCES app_system.users, title text NOT NULL, content text NOT NULL,
    index_version bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_memory.state (
    user_id uuid PRIMARY KEY REFERENCES app_system.users, snapshot_id uuid NOT NULL REFERENCES app_memory.snapshots,
    version bigint NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_memory.jobs (
    job_id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app_system.users,
    conversation_id uuid REFERENCES app_runtime.conversations, job_type text NOT NULL, status text NOT NULL,
    source_watermark uuid, lease_owner text, lease_until timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);

-- 审计身份标识是快照而非可级联删除的业务外键，账号与知识维护不会清掉历史。
CREATE TABLE app_audit.traces (
    trace_id uuid PRIMARY KEY, user_id uuid, conversation_id uuid, run_id uuid,
    service text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_audit.spans (
    span_id uuid PRIMARY KEY, trace_id uuid NOT NULL REFERENCES app_audit.traces,
    parent_span_id uuid, name text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (trace_id,span_id), FOREIGN KEY (trace_id,parent_span_id) REFERENCES app_audit.spans(trace_id,span_id)
);
CREATE TABLE app_audit.events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trace_id uuid NOT NULL, span_id uuid NOT NULL, user_id uuid, conversation_id uuid, run_id uuid, call_id text,
    event_type text NOT NULL, raw_json jsonb NOT NULL, usage_source text NOT NULL DEFAULT 'unavailable',
    input_tokens bigint, output_tokens bigint, total_tokens bigint, usage_method text, model text,
    duration_ms double precision CHECK (duration_ms >= 0), created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (trace_id,span_id) REFERENCES app_audit.spans(trace_id,span_id),
    CHECK (usage_source IN ('reported','estimated','unavailable')),
    CHECK (input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0),
    CHECK ((usage_source='unavailable' AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL)
       OR (usage_source IN ('reported','estimated') AND coalesce(input_tokens,output_tokens,total_tokens) IS NOT NULL)),
    CHECK (usage_source <> 'estimated' OR (usage_method IS NOT NULL AND model IS NOT NULL))
);
CREATE INDEX audit_trace_sequence ON app_audit.events(trace_id,event_id);
CREATE INDEX audit_user_time ON app_audit.events(user_id,created_at);
CREATE INDEX messages_conversation_time ON app_runtime.messages(conversation_id,created_at);

CREATE FUNCTION app_audit.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- 无调用参数；禁止原始记录变更并抛出异常，不返回可修改的记录。
    RAISE EXCEPTION '原始记录仅可追加，禁止 %', TG_OP USING ERRCODE='42501';
END;
$$;

DO $$
DECLARE
    -- 这些表保存原始历史，只能通过追加新记录表达变化。
    target_table text;
BEGIN
    FOREACH target_table IN ARRAY ARRAY['app_audit.traces','app_audit.spans','app_audit.events',
        'app_runtime.messages','app_runtime.run_events','app_runtime.checkpoints','app_runtime.context_summaries',
        'app_knowledge.reviews','app_memory.extractions','app_memory.snapshots']
    LOOP
        EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION app_audit.reject_mutation()',target_table);
        EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION app_audit.reject_mutation()',target_table);
    END LOOP;
END;
$$;
