-- 外置虚构部门数据；只在显式 seed 命令中加载。
INSERT INTO app_system.departments(department_id,code,name) VALUES
('D1','management','管理部'),
('D2','tech','技术部'),
('D3','hr','人事部'),
('D4','legal','法务部') ON CONFLICT DO NOTHING;
INSERT INTO demo_business.departments(department_id,code,name)
SELECT department_id,code,name FROM app_system.departments ON CONFLICT DO NOTHING;
