import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { Check, ChevronLeft, ChevronRight, Download, Eye, FileText, RefreshCw, Upload, X } from 'lucide-react';
import { apiRequest, beijingTime, knowledgeBlob, uploadKnowledge, type UserProfile } from './api';

interface Entry {
  application_id: string; creator_user_id: string; creator_name?: string; filename: string; file_format: string; byte_size: number;
  status: string; parse_status: string; minimum_grade: number | null; allowed_department_ids: string[]; fingerprint: string;
  created_at: string; updated_at: string; document_id: string | null; can_preview: boolean; can_delete: boolean; can_withdraw: boolean;
  can_review: boolean; can_retry: boolean; error_json?: {code: string; message: string} | null;
}
interface Preview { format: string; page_count: number; text: string; pages?: {page: number; text: string}[]; blocks?: {type: string; text?: string; style?: string; rows?: string[][]}[]; }
interface Detail extends Entry { reviews: {review_id: string; decision: string; comment: string; created_at: string}[]; jobs: {job_id: string; job_type: string; status: string; error_json?: {message: string}}[]; }
interface Options { departments: {department_id: string; name: string}[]; max_files: number; max_file_bytes: number; }
interface UploadResult { filename: string; success: boolean; error?: {message: string}; application?: Entry; }
const statuses: Record<string,string> = {pending_review:'待审核',approved_pending_ingest:'批准待处理',parsing:'解析中',pending_index:'待索引',rejected:'已驳回',failed:'处理失败',withdrawn:'已撤回',deleting:'删除中',invalidated:'已失效'};

/** 接收当前可信资料，渲染本人提交、管理员审核和真实原件预览；不提供 ACL 修改或假检索状态。 */
export function KnowledgePage({user}: {user: UserProfile}) {
  const [options,setOptions] = useState<Options | null>(null);
  const [scope,setScope] = useState<'mine'|'all'>('mine');
  const [pendingOnly,setPendingOnly] = useState(false);
  const [rows,setRows] = useState<Entry[]>([]);
  const [files,setFiles] = useState<File[]>([]);
  const [departments,setDepartments] = useState<string[]>([]);
  const [grade,setGrade] = useState('');
  const [uploading,setUploading] = useState(false);
  const [results,setResults] = useState<UploadResult[]>([]);
  const [notice,setNotice] = useState('');
  const [error,setError] = useState('');
  const [selected,setSelected] = useState<Detail | null>(null);
  const [preview,setPreview] = useState<Preview | null>(null);
  const [page,setPage] = useState(1);
  // PDF 图片保持真实页面内容，通过本地缩放提高侧栏和小屏阅读，不绕过单页鉴权。
  const [zoom,setZoom] = useState(100);
  const [image,setImage] = useState('');
  const [previewBusy,setPreviewBusy] = useState(false);
  const [previewError,setPreviewError] = useState('');
  const [actionBusy,setActionBusy] = useState(false);
  const [comment,setComment] = useState('');
  const [deleteConfirm,setDeleteConfirm] = useState(false);

  /** 无参数读取当前范围的管理目录，保留选中材料的最新状态；失效或撤权立即收起已加载正文。 */
  const reload = useCallback(async () => {
    try {
      const data = await apiRequest<Entry[]>(`/knowledge/applications?scope=${scope}`);
      setRows(data); setError('');
      setSelected(current => {
        if (!current) return current;
        const updated = data.find(item=>item.application_id===current.application_id);
        if (!updated) return null;
        if (!updated.can_preview) setPreview(null);
        return {...current,...updated};
      });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '读取材料失败'); }
  },[scope]);

  useEffect(() => { void reload();const timer=setInterval(()=>void reload(),2000);return()=>clearInterval(timer); },[reload]);
  useEffect(() => {
    setPreview(null);setImage('');setPreviewError('');
    if (user.can_use_knowledge) apiRequest<Options>('/knowledge/options').then(setOptions).catch(reason=>setError(reason.message));
    else setOptions(null);
  },[user.permission_version,user.can_use_knowledge]);
  useEffect(()=>{if(user.role!=='admin' && scope==='all'){setScope('mine');setRows([]);setSelected(null);setPreview(null);}},[user.role,scope]);

  useEffect(() => {
    if (!selected || !preview || preview.format!=='pdf') { setImage(''); return; }
    const controller = new AbortController();
    let objectUrl = '';
    setImage('');setPreviewError('');
    knowledgeBlob(`/knowledge/applications/${selected.application_id}/pages/${page}`,controller.signal).then(blob=>{
      if (!controller.signal.aborted) {objectUrl=URL.createObjectURL(blob);setImage(objectUrl);}
    }).catch(reason=>{if(!controller.signal.aborted)setPreviewError(reason instanceof Error ? reason.message : 'PDF 页面读取失败');});
    return()=>{controller.abort();if(objectUrl)URL.revokeObjectURL(objectUrl);};
  },[selected?.application_id,selected?.can_preview,preview,page]);

  /** 接收提交事件，将每份原件和统一 ACL 发给服务端，逐文件展示真实结果，保持失败项可重试。 */
  async function submit(event: FormEvent) {
    event.preventDefault();if(!files.length || uploading)return;
    if (options && files.length>options.max_files) {setError(`一次最多提交 ${options.max_files} 个文件`);return;}
    setUploading(true);setError('');setNotice('');setResults([]);
    const form = new FormData();
    form.set('metadata',JSON.stringify({allowed_department_ids:departments,minimum_grade:grade ? Number(grade) : null}));
    files.forEach(file=>form.append('files',file));
    try {
      const value = await uploadKnowledge<{results: UploadResult[]}>(form);
      setResults(value.results);setNotice(`已提交 ${value.results.filter(item=>item.success).length} 份，失败 ${value.results.filter(item=>!item.success).length} 份`);
      await reload();
    } catch(reason){setError(reason instanceof Error ? reason.message : '提交失败');}
    finally{setUploading(false);}
  }

  /** 接收选中材料，读取真实管理履历，清除上一份正文与删除确认，不因创建者身份自动读取受限正文。 */
  async function select(entry: Entry) {
    setPreview(null);setPreviewError('');setDeleteConfirm(false);setComment('');setPage(1);
    try {setSelected(await apiRequest<Detail>(`/knowledge/applications/${entry.application_id}`));}
    catch(reason){setError(reason instanceof Error ? reason.message : '无法读取材料');}
  }

  /** 无参数读取已授权解析正文，失败显示服务器原因；PDF 后续每页仍单独鉴权。 */
  async function openPreview() {
    if(!selected)return;
    setPreviewBusy(true);setPreviewError('');setPreview(null);setPage(1);setZoom(100);
    try {setPreview(await apiRequest<Preview>(`/knowledge/applications/${selected.application_id}/preview`));}
    catch(reason){setPreviewError(reason instanceof Error ? reason.message : '预览失败');}
    finally{setPreviewBusy(false);}
  }

  /** 接收批准或驳回决定，提交当前材料指纹及意见；服务器只决策，不接收修改 ACL 的字段。 */
  async function review(decision: 'approved'|'rejected') {
    if(!selected)return;
    setActionBusy(true);setError('');
    try {await apiRequest(`/knowledge/applications/${selected.application_id}/review`,'POST',{decision,expected_fingerprint:selected.fingerprint,comment});setNotice(decision==='approved' ? '已批准，等待后台处理；检索索引将在下一阶段接入。' : '已驳回申请');await reload();await select(selected);}
    catch(reason){setError(reason instanceof Error ? reason.message : '审核失败');}
    finally{setActionBusy(false);}
  }

  /** 接收撤回、删除或重试动作，调用本人/管理员维护接口并刷新真实状态，不删除历史审计。 */
  async function maintain(action: 'withdraw'|'delete'|'retry') {
    if(!selected)return;
    setActionBusy(true);setError('');
    try{await apiRequest(`/knowledge/applications/${selected.application_id}/${action}`,'POST');setNotice(action==='delete' ? '材料已停止正文访问，后台正在完成删除状态。' : action==='withdraw' ? '申请已撤回。' : '已重新排入处理任务。');setDeleteConfirm(false);await reload();await select(selected);}
    catch(reason){setError(reason instanceof Error ? reason.message : '操作失败');}
    finally{setActionBusy(false);}
  }

  /** 无参数鉴权下载当前原件，临时 Blob 链接完成后释放；不使用 MinIO 直链。 */
  async function download() {
    if(!selected)return;
    try{const blob=await knowledgeBlob(`/knowledge/applications/${selected.application_id}/download`);const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=selected.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
    catch(reason){setPreviewError(reason instanceof Error ? reason.message : '下载失败');}
  }

  const names = (ids:string[]) => ids.length ? ids.map(id=>options?.departments.find(item=>item.department_id===id)?.name || '受限部门').join('、') : '部门不限';
  const shown = pendingOnly ? rows.filter(row=>row.status==='pending_review') : rows;
  return <section className="knowledge-page">
    <div className="knowledge-heading"><div><h1>知识库管理</h1><p>提交材料、确认访问范围，查看审核与处理结果。</p></div><button onClick={()=>void reload()}><RefreshCw size={17} />刷新列表</button></div>
    {!user.can_use_knowledge ? <p className="knowledge-banner">尚未获得知识库权限，请联系管理员分配部门和职级。既有申请的管理信息仍可查看。</p> : <details className="knowledge-upload" open>
      <summary><Upload size={19} />{user.role==='admin' ? '上传并直接导入' : '提交文件审核'}</summary>
      <form onSubmit={submit}><label htmlFor="knowledge-files">文件（PDF、DOCX、TXT、Markdown）</label><input id="knowledge-files" type="file" multiple accept=".pdf,.docx,.txt,.md,.markdown" disabled={uploading} onChange={event=>setFiles(Array.from(event.target.files || []))}/>
        <div className="knowledge-acl"><fieldset><legend>允许部门（不选表示不限）</legend><div>{options?.departments.map(item=><label key={item.department_id}><input type="checkbox" value={item.department_id} checked={departments.includes(item.department_id)} disabled={uploading} onChange={event=>setDepartments(current=>event.target.checked ? [...current,item.department_id] : current.filter(id=>id!==item.department_id))}/>{item.name}</label>)}</div></fieldset>
          <label htmlFor="knowledge-grade">最低职级<select id="knowledge-grade" disabled={uploading} value={grade} onChange={event=>setGrade(event.target.value)}><option value="">不限</option>{Array.from({length:10},(_,index)=><option key={index+1} value={index+1}>{index+1}</option>)}</select></label></div>
        <p className="knowledge-help">{options ? `每次最多 ${options.max_files} 份，每份最多 ${Math.round(options.max_file_bytes/1024/1024)} MB。` : '正在读取上传限制。'} 文件独立处理；申请 ACL 不能由审核者暗改。</p>
        {!user.role.startsWith('admin') && (grade && Number(grade)>(user.grade || 0) || departments.length>0 && !departments.includes(user.department_id || '')) && <p className="knowledge-banner">此访问范围不包含你；提交后可管理申请，但不能预览或下载正文。</p>}
        <button className="knowledge-primary" disabled={uploading || !files.length || !options}>{uploading ? '正在提交…' : user.role==='admin' ? '确认并导入' : '提交审核'}</button>
      </form>{results.length>0 && <ul className="upload-results">{results.map((item,index)=><li key={index} className={item.success ? '' : 'error-text'}>{item.success ? <Check size={16}/> : <X size={16}/>}<span>{item.filename}：{item.success ? statuses[item.application?.status || ''] : item.error?.message}</span></li>)}</ul>}
    </details>}
    {notice && <p role="status" className="knowledge-notice">{notice}</p>}{error && <p role="alert" className="error-text">{error}</p>}
    <div className="knowledge-tabs"><button aria-pressed={scope==='mine'} onClick={()=>{setScope('mine');setPendingOnly(false);setSelected(null);setPreview(null);}}>我的提交</button>{user.role==='admin' && <><button aria-pressed={scope==='all' && !pendingOnly} onClick={()=>{setScope('all');setPendingOnly(false);setSelected(null);setPreview(null);}}>全部材料</button><button aria-pressed={scope==='all' && pendingOnly} onClick={()=>{setScope('all');setPendingOnly(true);setSelected(null);setPreview(null);}}>待审核</button></>}</div>
    <div className={`knowledge-main ${selected ? 'with-preview' : ''}`}><div className="knowledge-list">{shown.length===0 && <p className="knowledge-empty">此范围暂无材料。上传后的审核与处理状态会显示在这里。</p>}{shown.map(row=><article key={row.application_id} className={selected?.application_id===row.application_id ? 'knowledge-row selected' : 'knowledge-row'}>
      <FileText size={22}/><div><h2>{row.filename}</h2><p>{names(row.allowed_department_ids)} · {row.minimum_grade ? `职级 ≥ ${row.minimum_grade}` : '职级不限'} · {beijingTime(row.created_at)}{scope==='all' && row.creator_name ? ` · ${row.creator_name}` : ''}</p><span className={`knowledge-status ${row.status}`}>{statuses[row.status] || row.status}</span>{row.status==='pending_review' && <span className="knowledge-parse-state">{row.parse_status==='completed' ? '预览已就绪' : row.parse_status==='failed' ? '预览解析失败' : '预览处理中'}</span>}{row.error_json && <p className="error-text">{row.error_json.message}</p>}</div><button aria-label={`查看 ${row.filename}`} onClick={()=>void select(row)}>查看</button></article>)}</div>
      {selected && <aside className="knowledge-detail" aria-label="知识材料详情"><div className="knowledge-detail-heading"><h2>{selected.filename}</h2><button aria-label="关闭材料详情" onClick={()=>{setSelected(null);setPreview(null);}}><X size={20}/></button></div>
        <p>{statuses[selected.status]} · {names(selected.allowed_department_ids)} · {selected.minimum_grade ? `职级 ≥ ${selected.minimum_grade}` : '职级不限'}</p>
        {selected.status==='pending_index' && <p className="knowledge-banner">原件已解析并批准，当前待索引；向量检索将在第六阶段接入。</p>}
        {!selected.can_preview && <p className="knowledge-banner">当前权限或材料状态不允许读取正文；管理信息仍可查看。</p>}
        <div className="knowledge-actions"><button disabled={!selected.can_preview || previewBusy} onClick={()=>void openPreview()}><Eye size={17}/>{previewBusy ? '正在读取…' : '预览正文'}</button><button disabled={!selected.can_preview} onClick={()=>void download()}><Download size={17}/>下载原件</button>{selected.can_withdraw && <button disabled={actionBusy} onClick={()=>void maintain('withdraw')}>撤回</button>}{selected.can_retry && <button disabled={actionBusy} onClick={()=>void maintain('retry')}>重试处理</button>}{selected.can_delete && <button disabled={actionBusy} onClick={()=>setDeleteConfirm(true)}>删除</button>}</div>
        {deleteConfirm && <div className="knowledge-delete-confirm"><p>删除后立即停止正文访问，原始审核和审计保留。确认删除此材料？</p><button disabled={actionBusy} onClick={()=>void maintain('delete')}>确认删除</button><button onClick={()=>setDeleteConfirm(false)}>取消</button></div>}
        {selected.can_review && <div className="knowledge-review"><label htmlFor="knowledge-review-comment">审核意见<textarea id="knowledge-review-comment" maxLength={2000} value={comment} onChange={event=>setComment(event.target.value)}/></label><p>只通过或驳回当前材料与访问范围，不修改申请 ACL。</p><div><button className="knowledge-primary" disabled={actionBusy || selected.parse_status!=='completed'} onClick={()=>void review('approved')}>通过</button><button disabled={actionBusy} onClick={()=>void review('rejected')}>驳回</button></div></div>}
        {previewError && <p role="alert" className="error-text">{previewError}</p>}
        {preview && <div className="knowledge-preview">{preview.format==='pdf' ? <><div className="pdf-toolbar"><button aria-label="上一页" disabled={page<=1} onClick={()=>setPage(value=>value-1)}><ChevronLeft size={18}/></button><span>第 {page} / {preview.page_count} 页</span><button aria-label="下一页" disabled={page>=preview.page_count} onClick={()=>setPage(value=>value+1)}><ChevronRight size={18}/></button><button aria-label="缩小 PDF" disabled={zoom<=50} onClick={()=>setZoom(value=>value-25)}>−</button><span>{zoom}%</span><button aria-label="放大 PDF" disabled={zoom>=250} onClick={()=>setZoom(value=>value+25)}>+</button></div>{image ? <div className="pdf-image-scroll"><img className="pdf-page-image" style={{width:`${zoom}%`}} src={image} alt={`PDF 第 ${page} 页`}/></div> : !previewError && <p>正在读取 PDF 页面…</p>}<details><summary>此页解析文字</summary><pre>{preview.pages?.find(item=>item.page===page)?.text}</pre></details></> : preview.blocks?.map((block,index)=>block.type==='table' ? <div className="docx-table" key={index}><table><tbody>{block.rows?.map((row,i)=><tr key={i}>{row.map((cell,j)=><td key={j}>{cell}</td>)}</tr>)}</tbody></table></div> : <p key={index} className={block.style?.match(/Heading|标题/i) ? 'docx-heading' : 'parsed-text'}>{block.text}</p>)}</div>}
        {selected.reviews.length>0 && <div className="knowledge-history"><h3>审核记录</h3>{selected.reviews.map(item=><p key={item.review_id}>{item.decision==='approved' ? '已通过' : '已驳回'} · {beijingTime(item.created_at)}{item.comment ? `：${item.comment}` : ''}</p>)}</div>}
      </aside>}
    </div>
  </section>;
}
