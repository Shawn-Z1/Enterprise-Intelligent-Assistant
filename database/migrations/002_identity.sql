-- M03 会话防护与职员资料；不改变已有账号密码或授权。
ALTER TABLE app_system.auth_sessions ADD COLUMN csrf_token_hash text;
ALTER TABLE app_system.users ADD COLUMN position text;
UPDATE app_system.users u SET position=e.position
FROM demo_business.employees e WHERE e.user_id=u.user_id AND e.employee_id=u.employee_id;
CREATE INDEX auth_sessions_user_expiry ON app_system.auth_sessions(user_id,expires_at);
CREATE TABLE app_system.login_limits (
    bucket_key text PRIMARY KEY, attempts integer NOT NULL CHECK (attempts > 0),
    window_start timestamptz NOT NULL DEFAULT now()
);
