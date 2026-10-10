-- M07复用批准表，扩展为敏感动作级确认；保留历史查询批准和检查点。
ALTER TABLE app_runtime.query_approvals ADD COLUMN action_type text NOT NULL DEFAULT 'business.query';
ALTER TABLE app_runtime.query_approvals ADD COLUMN conversation_id varchar(16) REFERENCES app_runtime.conversations;
ALTER TABLE app_runtime.query_approvals ADD COLUMN payload_json jsonb NOT NULL DEFAULT '{}';
ALTER TABLE app_runtime.query_approvals ADD COLUMN policy_version integer NOT NULL DEFAULT 1;
ALTER TABLE app_runtime.query_approvals ADD COLUMN execution_token_hash text;
ALTER TABLE app_runtime.query_approvals ADD COLUMN result_json jsonb;
ALTER TABLE app_runtime.query_approvals ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
CREATE UNIQUE INDEX action_per_call ON app_runtime.query_approvals(call_id);
DROP INDEX app_runtime.one_active_run_per_conversation;
CREATE UNIQUE INDEX one_active_run_per_conversation ON app_runtime.runs(conversation_id) WHERE status IN ('pending','running','waiting_confirmation');
CREATE UNIQUE INDEX graph_checkpoint_version ON app_runtime.checkpoints(run_id,(state_json->>'namespace'),(state_json->>'graph_id'));
CREATE TABLE app_runtime.graph_writes (
    run_id varchar(16) NOT NULL REFERENCES app_runtime.runs,
    namespace text NOT NULL, checkpoint_version text NOT NULL, task_id text NOT NULL,
    position integer NOT NULL, channel text NOT NULL, payload_json jsonb NOT NULL,
    PRIMARY KEY(run_id,namespace,checkpoint_version,task_id,position)
);

-- 随机执行能力只在消费批准后生成。业务读账号既不能读批准表，也不能伪造有效能力。
CREATE FUNCTION demo_business.authorized_query_scope()
RETURNS TABLE(employee_id varchar, department_name text, grade smallint, is_admin boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
 SELECT u.employee_id,d.name,u.grade,u.role='admin'
 FROM app_runtime.query_approvals a JOIN app_runtime.runs r ON r.run_id=a.run_id
 JOIN app_system.users u ON u.user_id=a.user_id
 JOIN app_system.auth_sessions s ON s.session_id=r.state_json->>'auth_session_id' AND s.user_id=u.user_id
 LEFT JOIN app_system.departments d ON d.department_id=u.department_id
 WHERE a.action_type='business.query' AND a.decision='executing' AND a.consumed_at IS NOT NULL
 AND a.execution_token_hash=encode(sha256(convert_to(current_setting('zhixing.action_token',true),'UTF8')),'hex')
 AND r.status='running' AND NOT r.cancel_requested AND u.status='active' AND u.business_query_enabled
 AND u.permission_version=a.permission_version AND s.revoked_at IS NULL AND s.expires_at>now()
 AND a.expires_at>now()
$$;
CREATE VIEW demo_business.visible_departments WITH (security_barrier=true) AS
 SELECT t.* FROM demo_business.departments t WHERE EXISTS(SELECT 1 FROM demo_business.authorized_query_scope());
CREATE VIEW demo_business.visible_employees WITH (security_barrier=true) AS
 SELECT t.employee_id,t.name,t.department_id,t.position,t.grade,t.hire_date,t.status,t.work_email,t.contact_phone
 FROM demo_business.employees t CROSS JOIN demo_business.authorized_query_scope() s WHERE s.is_admin OR t.employee_id=s.employee_id;
CREATE VIEW demo_business.visible_attendance_records WITH (security_barrier=true) AS
 SELECT t.* FROM demo_business.attendance_records t CROSS JOIN demo_business.authorized_query_scope() s WHERE s.is_admin OR t.employee_id=s.employee_id;
CREATE VIEW demo_business.visible_tickets WITH (security_barrier=true) AS
 SELECT t.* FROM demo_business.tickets t CROSS JOIN demo_business.authorized_query_scope() s
 WHERE s.is_admin OR (s.department_name='技术部' AND s.grade>=3 AND (t.submitter_employee_id=s.employee_id OR t.assignee_employee_id=s.employee_id));
CREATE VIEW demo_business.visible_approval_records WITH (security_barrier=true) AS
 SELECT t.* FROM demo_business.approval_records t CROSS JOIN demo_business.authorized_query_scope() s
 WHERE s.is_admin OR (((s.department_name='人事部' AND s.grade>=5) OR (s.department_name='法务部' AND s.grade>=6))
 AND (t.applicant_employee_id=s.employee_id OR t.handler_employee_id=s.employee_id));
