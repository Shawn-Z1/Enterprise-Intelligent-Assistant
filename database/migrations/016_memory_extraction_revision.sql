-- 提取策略更新允许有界重提取，原候选/快照不被修改；同一run同策略仍幂等。
ALTER TABLE app_memory.jobs ADD COLUMN processing_version integer NOT NULL DEFAULT 1;
ALTER TABLE app_memory.extractions ADD COLUMN processing_version integer NOT NULL DEFAULT 1;
DROP INDEX app_memory.memory_extract_run;
DROP INDEX app_memory.memory_extraction_watermark;
CREATE UNIQUE INDEX memory_extract_run ON app_memory.jobs(run_id,processing_version) WHERE job_type='extract';
CREATE UNIQUE INDEX memory_extraction_watermark ON app_memory.extractions(user_id,conversation_id,message_watermark,processing_version);
