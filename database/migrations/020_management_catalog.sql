-- 管理目录清理仅隐藏已失效申请，不删除原件、聊天引用或不可变审计。
ALTER TABLE app_knowledge.applications ADD COLUMN management_deleted_at timestamptz;
ALTER TABLE app_knowledge.applications ADD COLUMN management_deleted_by varchar(16) REFERENCES app_system.users(user_id);
CREATE INDEX knowledge_management_catalog ON app_knowledge.applications(updated_at DESC,application_id)
    WHERE management_deleted_at IS NULL;
