-- 补齐新安装的职员岗位；仅首次导入时处理未填写值，不覆盖管理员维护的资料。
UPDATE app_system.users u SET position=e.position
FROM demo_business.employees e WHERE e.user_id=u.user_id AND e.employee_id=u.employee_id AND u.position IS NULL;
