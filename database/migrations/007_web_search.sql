-- 公开查询与来源采用有所有者的永久快照，不充当跨用户搜索缓存。
CREATE TABLE app_runtime.web_searches (
    search_id varchar(16) PRIMARY KEY,
    user_id varchar(16) NOT NULL REFERENCES app_system.users,
    conversation_id varchar(16) NOT NULL REFERENCES app_runtime.conversations,
    run_id varchar(16) NOT NULL REFERENCES app_runtime.runs,
    query text NOT NULL,
    status text NOT NULL,
    response_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX web_search_user_time ON app_runtime.web_searches(user_id,created_at);
CREATE TABLE app_runtime.web_sources (
    source_id varchar(16) PRIMARY KEY,
    search_id varchar(16) NOT NULL REFERENCES app_runtime.web_searches,
    snapshot_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
