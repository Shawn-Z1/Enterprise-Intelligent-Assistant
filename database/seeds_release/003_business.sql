-- 公开版仅管理员可登录；其他虚构职员不关联登录身份，不创建成员账号。
-- 外置模拟业务五表的关联数据；均为虚构，时间固定便于复测。
INSERT INTO demo_business.employees(employee_id,user_id,name,department_id,position,grade,hire_date,status,work_email,contact_phone) VALUES
('E1','U1','系统管理员','D1','系统管理',10,'2020-01-01','active','admin@example.invalid','13800000001'),
('E2',NULL,'张三','D2','开发工程师',3,'2023-03-15','active','zhangsan@example.invalid','13800000002'),
('E3',NULL,'李四','D3','人事专员',5,'2021-06-01','active','lisi@example.invalid','13800000003'),
('E4',NULL,'王五','D4','法务专员',6,'2022-02-10','active','wangwu@example.invalid','13800000004'),
('E5',NULL,'赵六','D2','技术负责人',9,'2019-08-20','active','zhaoliu@example.invalid','13800000005')
ON CONFLICT DO NOTHING;

INSERT INTO demo_business.attendance_records(attendance_id,employee_id,attendance_date,check_in,check_out,status,late_minutes,absent_minutes)
SELECT 'A' || ((substr(e.employee_id,2)::integer-1)*5+(day::date-'2026-09-28'::date)+1)::text,e.employee_id,day::date,
       day::date + time '09:00' AT TIME ZONE 'Asia/Shanghai',
       day::date + time '18:00' AT TIME ZONE 'Asia/Shanghai','present',0,0
FROM demo_business.employees e CROSS JOIN generate_series('2026-09-28'::date,'2026-10-02'::date,interval '1 day') day
ON CONFLICT DO NOTHING;

INSERT INTO demo_business.approval_records(approval_id,applicant_employee_id,handler_employee_id,approval_type,title,status,created_at,processed_at) VALUES
('P1','E2','E3','leave','张三年假申请','approved','2026-09-28 10:00+08','2026-09-28 14:00+08'),
('P2','E3','E4','contract','招聘合同审核','pending','2026-09-29 11:00+08',NULL),
('P3','E4','E3','expense','法务材料费用申请','approved','2026-09-30 09:30+08','2026-09-30 16:00+08')
ON CONFLICT DO NOTHING;

INSERT INTO demo_business.tickets(ticket_id,submitter_employee_id,assignee_employee_id,category,priority,status,created_at,completed_at,description) VALUES
('T1','E2','E5','application','high','open','2026-09-28 10:00+08',NULL,'演示：修复报表页面加载问题'),
('T2','E3','E2','support','normal','completed','2026-09-29 09:00+08','2026-09-29 15:00+08','演示：调整人事表单字段'),
('T3','E5','E2','application','normal','in_progress','2026-09-30 10:00+08',NULL,'演示：完善知识资料目录')
ON CONFLICT DO NOTHING;
