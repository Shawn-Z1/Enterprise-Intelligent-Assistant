-- 全部为虚构数据：2026-09-07至09-30的18个工作日、六位代码平台作者、四个仓库。
-- 固定算法产生非均匀提交量/时间/改动量，保留幂等主键；不读取真实Git或重置员工账号。
WITH authors(idx,username,name,employee_id,focus) AS (VALUES
 (1,'zhangsan','张三','E2','backend'),
 (2,'zhaoliu','赵六','E5','architecture'),
 (3,'chenchen','陈晨',NULL,'frontend'),
 (4,'linyue','林悦',NULL,'quality'),
 (5,'xuhang','许航',NULL,'operations'),
 (6,'zhouning','周宁',NULL,'data')
), days AS (
 SELECT day::date AS day,row_number() OVER(ORDER BY day)::integer AS day_no
 FROM generate_series('2026-09-07'::date,'2026-09-30'::date,interval '1 day') AS day
 WHERE extract(isodow FROM day)<6
), commits AS (
 SELECT d.*,a.*,n,
 CASE a.focus WHEN 'frontend' THEN 'platform-web' WHEN 'operations' THEN 'infra-config'
 WHEN 'data' THEN 'knowledge-indexer' WHEN 'quality' THEN CASE WHEN d.day_no%2=0 THEN 'platform-web' ELSE 'agent-service' END
 WHEN 'architecture' THEN CASE WHEN d.day_no%3=0 THEN 'infra-config' ELSE 'agent-service' END ELSE 'agent-service' END AS repo,
 (d.day_no+a.idx+n)%6 AS kind_no
 FROM days d CROSS JOIN authors a
 CROSS JOIN LATERAL generate_series(1,1+(d.day_no*a.idx+a.idx)%4) AS n
 WHERE (d.day_no+a.idx)%11<>0
)
INSERT INTO demo_business.code_submissions(
 submission_id,author_username,author_name,author_employee_id,repository,branch,commit_sha,submitted_at,
 version,commit_type,message,files_changed,lines_added,lines_deleted,pull_request_no,review_status,build_status,build_duration_seconds)
SELECT 'CS'||to_char(day,'YYMMDD')||lpad(idx::text,2,'0')||lpad(n::text,2,'0'),username,name,employee_id,
 repo,CASE WHEN kind_no=1 THEN 'fix/' ELSE 'feature/' END || (ARRAY['rag-index','session-guard','markdown-reply','query-chart','ci-cache','retrieval-metrics'])[(day_no+idx)%6+1],
 substr(md5(day::text||username||n::text)||md5(repo||day_no::text),1,40),
 (day+make_time(9+(idx+n+day_no)%10,(day_no*13+idx*7+n*11)%60,0)) AT TIME ZONE 'Asia/Shanghai',
 'v0.7.'||((day_no-1)/5)::text,
 (ARRAY['feat','fix','refactor','test','docs','chore'])[kind_no+1],
 (ARRAY['完善混合检索索引任务及错误回执','修复登录会话失效后的页面恢复','整理Markdown回复和来源面板布局','补充业务查询确认及图表测试','调整CI依赖缓存与部署检查','优化检索统计和慢查询观测','修复表格小屏滚动与焦点状态','补充只读SQL参数绑定和边界验证','重构模型网关超时及用量记录','更新开发说明和接口示例','补充代码审查后的回归覆盖','优化任务队列领取及中断恢复'])[(day_no+idx*2+n)%12+1],
 1+(day_no*3+idx+n)%12,8+(day_no*29+idx*47+n*61)%480,(day_no*11+idx*19+n*23)%180,
 700+day_no*10+idx,
 CASE WHEN day_no>=17 AND idx%3=0 THEN 'pending' WHEN (day_no+idx)%13=0 THEN 'changes_requested' ELSE 'approved' END,
 CASE WHEN day_no=18 AND n=1 AND idx IN (3,6) THEN 'running' WHEN (day_no+idx+n)%17=0 THEN 'failed' ELSE 'passed' END,
 CASE WHEN day_no=18 AND n=1 AND idx IN (3,6) THEN NULL ELSE 45+(day_no*17+idx*31+n*13)%420 END
FROM commits ORDER BY day,idx,n
ON CONFLICT DO NOTHING;
