-- 技术部代码提交共享记录，作者快照来自代码平台，不依赖员工个人资料视图做人员统计。
CREATE TABLE demo_business.code_submissions (
    submission_id varchar(16) PRIMARY KEY,
    author_username text NOT NULL,
    author_name text NOT NULL,
    author_employee_id varchar(16) REFERENCES demo_business.employees(employee_id),
    repository text NOT NULL,
    branch text NOT NULL,
    commit_sha varchar(40) NOT NULL UNIQUE CHECK(commit_sha ~ '^[0-9a-f]{40}$'),
    submitted_at timestamptz NOT NULL,
    version text NOT NULL,
    commit_type text NOT NULL CHECK(commit_type IN ('feat','fix','refactor','test','docs','chore')),
    message text NOT NULL,
    files_changed integer NOT NULL CHECK(files_changed>0),
    lines_added integer NOT NULL CHECK(lines_added>=0),
    lines_deleted integer NOT NULL CHECK(lines_deleted>=0),
    pull_request_no integer NOT NULL,
    review_status text NOT NULL CHECK(review_status IN ('approved','pending','changes_requested')),
    build_status text NOT NULL CHECK(build_status IN ('passed','failed','running')),
    build_duration_seconds integer CHECK(build_duration_seconds>=0)
);
COMMENT ON TABLE demo_business.code_submissions IS '外置虚构代码提交演示数据；技术部有业务资格的成员可读全部作者与全部记录，不按本人过滤';
COMMENT ON COLUMN demo_business.code_submissions.author_employee_id IS '已关联平台员工时保存编号；其余代码平台作者保留姓名/用户名快照，不创建登录账号';
CREATE INDEX code_submissions_date_author ON demo_business.code_submissions(submitted_at,author_username);
CREATE VIEW demo_business.visible_code_submissions WITH (security_barrier=true) AS
 SELECT t.* FROM demo_business.code_submissions t
 WHERE EXISTS(SELECT 1 FROM demo_business.authorized_query_scope() s
              WHERE s.is_admin OR s.department_name='技术部');
