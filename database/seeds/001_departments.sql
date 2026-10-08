-- 外置虚构部门数据；只在显式 seed 命令中加载。
INSERT INTO app_system.departments(department_id,code,name) VALUES
('10000000-0000-0000-0000-000000000001','management','管理部'),
('10000000-0000-0000-0000-000000000002','tech','技术部'),
('10000000-0000-0000-0000-000000000003','hr','人事部'),
('10000000-0000-0000-0000-000000000004','legal','法务部') ON CONFLICT DO NOTHING;
INSERT INTO demo_business.departments(department_id,code,name)
SELECT department_id,code,name FROM app_system.departments ON CONFLICT DO NOTHING;
