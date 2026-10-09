-- 切片策略 v2：继承完整标题路径、正文与实际页集合；保留旧切片和原审批/解析档案。
ALTER TABLE app_knowledge.chunks ADD COLUMN heading_path jsonb NOT NULL DEFAULT '[]';
ALTER TABLE app_knowledge.chunks ADD COLUMN section_paths jsonb NOT NULL DEFAULT '[]';
ALTER TABLE app_knowledge.chunks ADD COLUMN page_numbers integer[] NOT NULL DEFAULT '{}';
ALTER TABLE app_knowledge.chunks ADD COLUMN body_content text;
ALTER TABLE app_knowledge.chunks ADD COLUMN chunk_strategy text;
