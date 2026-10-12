-- Runner副本共享心跳与执行额度，启动代次用于阻止重复节点和迟到写回。
CREATE TABLE app_runtime.sandbox_nodes (
 runner_id text PRIMARY KEY,instance_id varchar(16) NOT NULL,capacity integer NOT NULL CHECK(capacity BETWEEN 1 AND 16),
 manifest jsonb NOT NULL,draining boolean NOT NULL DEFAULT true,
 heartbeat_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL DEFAULT now()+interval '30 seconds'
);
ALTER TABLE app_runtime.sandbox_jobs ADD COLUMN node_instance_id varchar(16);
-- 失联不等于容器已销毁；持有额度直到原节点确认清理，避免释放后突破实际资源预算。
ALTER TABLE app_runtime.sandbox_jobs ADD COLUMN resources_released boolean NOT NULL DEFAULT true;
UPDATE app_runtime.sandbox_jobs SET resources_released=false WHERE status IN ('running','unknown');
CREATE INDEX sandbox_held_resources ON app_runtime.sandbox_jobs(runner_id,user_id) WHERE NOT resources_released;
