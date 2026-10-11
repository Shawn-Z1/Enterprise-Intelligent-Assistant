-- 会话分析附件与知识申请相互独立；身份和对象键只由服务端确定。
CREATE TABLE app_runtime.analysis_attachments (
 attachment_id varchar(16) PRIMARY KEY,user_id varchar(16) NOT NULL REFERENCES app_system.users(user_id),
 conversation_id varchar(16) NOT NULL REFERENCES app_runtime.conversations(conversation_id),
 filename text NOT NULL,format text NOT NULL CHECK(format IN ('csv','xlsx')),size_bytes bigint NOT NULL,
 original_sha256 text NOT NULL,object_key text NOT NULL UNIQUE,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_runtime.sandbox_attempts (
 attempt_id varchar(16) PRIMARY KEY,call_id varchar(16) NOT NULL UNIQUE REFERENCES app_runtime.tool_calls(call_id),
 run_id varchar(16) NOT NULL REFERENCES app_runtime.runs(run_id),user_id varchar(16) NOT NULL REFERENCES app_system.users(user_id),
 operation text NOT NULL,status text NOT NULL,request_fingerprint text NOT NULL,input_json jsonb NOT NULL,
 node_policy jsonb NOT NULL,result_json jsonb,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_runtime.sandbox_results (
 result_id varchar(16) PRIMARY KEY,attempt_id varchar(16) NOT NULL UNIQUE REFERENCES app_runtime.sandbox_attempts(attempt_id),
 user_id varchar(16) NOT NULL REFERENCES app_system.users(user_id),conversation_id varchar(16) NOT NULL REFERENCES app_runtime.conversations(conversation_id),
 result_json jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app_runtime.sandbox_artifacts (
 artifact_id varchar(16) PRIMARY KEY,result_id varchar(16) NOT NULL REFERENCES app_runtime.sandbox_results(result_id),
 filename text NOT NULL,size_bytes bigint NOT NULL,sha256 text NOT NULL,object_key text NOT NULL UNIQUE
);
