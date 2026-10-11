-- 附件随用户消息发送，后续轮次可读取本会话已发送原件；未发送草稿不进入模型。
ALTER TABLE app_runtime.analysis_attachments ADD COLUMN message_id varchar(16) REFERENCES app_runtime.messages(message_id);
ALTER TABLE app_runtime.analysis_attachments ADD COLUMN draft_removed boolean NOT NULL DEFAULT false;
CREATE INDEX analysis_attachment_message ON app_runtime.analysis_attachments(message_id);
-- 旧勾选模式的附件关联到首次实际发送的用户消息，不改历史正文、引用和审计。
UPDATE app_runtime.analysis_attachments a SET message_id=(
 SELECT m.message_id FROM app_runtime.messages m JOIN app_runtime.runs r USING(run_id)
 WHERE m.role='user' AND m.user_id=a.user_id AND m.conversation_id=a.conversation_id
 AND r.state_json->'attachment_ids' ? a.attachment_id
 ORDER BY m.message_order LIMIT 1
);
