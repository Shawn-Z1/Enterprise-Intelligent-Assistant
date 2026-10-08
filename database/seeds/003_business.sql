-- 外置模拟业务五表的关联数据；均为虚构，时间固定便于复测。
INSERT INTO demo_business.employees(employee_id,user_id,name,department_id,position,grade,hire_date,status,work_email,contact_phone) VALUES
('30000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000001','演示管理员','10000000-0000-0000-0000-000000000001','系统管理',10,'2020-01-01','active','admin@example.invalid','13800000001'),
('30000000-0000-0000-0000-000000000002','20000000-0000-0000-0000-000000000002','张三','10000000-0000-0000-0000-000000000002','开发工程师',3,'2023-03-15','active','zhangsan@example.invalid','13800000002'),
('30000000-0000-0000-0000-000000000003','20000000-0000-0000-0000-000000000003','李四','10000000-0000-0000-0000-000000000003','人事专员',5,'2021-06-01','active','lisi@example.invalid','13800000003'),
('30000000-0000-0000-0000-000000000004','20000000-0000-0000-0000-000000000004','王五','10000000-0000-0000-0000-000000000004','法务专员',6,'2022-02-10','active','wangwu@example.invalid','13800000004'),
('30000000-0000-0000-0000-000000000005','20000000-0000-0000-0000-000000000005','赵六','10000000-0000-0000-0000-000000000002','技术负责人',9,'2019-08-20','active','zhaoliu@example.invalid','13800000005')
ON CONFLICT DO NOTHING;

INSERT INTO demo_business.attendance_records(attendance_id,employee_id,attendance_date,check_in,check_out,status,late_minutes,absent_minutes)
SELECT md5(e.employee_id::text || day::date::text)::uuid,e.employee_id,day::date,
       day::date + time '09:00' AT TIME ZONE 'Asia/Shanghai',
       day::date + time '18:00' AT TIME ZONE 'Asia/Shanghai','present',0,0
FROM demo_business.employees e CROSS JOIN generate_series('2026-09-28'::date,'2026-10-02'::date,interval '1 day') day
ON CONFLICT DO NOTHING;

INSERT INTO demo_business.approval_records(approval_id,applicant_employee_id,handler_employee_id,approval_type,title,status,created_at,processed_at) VALUES
('40000000-0000-0000-0000-000000000001','30000000-0000-0000-0000-000000000002','30000000-0000-0000-0000-000000000003','leave','张三年假申请','approved','2026-09-28 10:00+08','2026-09-28 14:00+08'),
('40000000-0000-0000-0000-000000000002','30000000-0000-0000-0000-000000000003','30000000-0000-0000-0000-000000000004','contract','招聘合同审核','pending','2026-09-29 11:00+08',NULL),
('40000000-0000-0000-0000-000000000003','30000000-0000-0000-0000-000000000004','30000000-0000-0000-0000-000000000003','expense','法务材料费用申请','approved','2026-09-30 09:30+08','2026-09-30 16:00+08')
ON CONFLICT DO NOTHING;

INSERT INTO demo_business.tickets(ticket_id,submitter_employee_id,assignee_employee_id,category,priority,status,created_at,completed_at,description) VALUES
('50000000-0000-0000-0000-000000000001','30000000-0000-0000-0000-000000000002','30000000-0000-0000-0000-000000000005','application','high','open','2026-09-28 10:00+08',NULL,'演示：修复报表页面加载问题'),
('50000000-0000-0000-0000-000000000002','30000000-0000-0000-0000-000000000003','30000000-0000-0000-0000-000000000002','support','normal','completed','2026-09-29 09:00+08','2026-09-29 15:00+08','演示：调整人事表单字段'),
('50000000-0000-0000-0000-000000000003','30000000-0000-0000-0000-000000000005','30000000-0000-0000-0000-000000000002','application','normal','in_progress','2026-09-30 10:00+08',NULL,'演示：完善知识资料目录')
ON CONFLICT DO NOTHING;
