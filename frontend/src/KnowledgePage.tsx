import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Ban, Check, ChevronLeft, ChevronRight, Download, Eye, FileText, FilterX, LoaderCircle, MoreHorizontal, RefreshCw, Search, Trash2, Upload, X } from 'lucide-react';
import { apiRequest, beijingTime, knowledgeBlob, uploadKnowledge, type UserProfile } from './api';
import { FileBadge, ManagementConfirm, ManagementDrawer, ManagementPagination } from './ManagementUI';

interface Entry {
  application_id:string;creator_user_id:string;creator_name?:string;filename:string;file_format:string;byte_size:number;status:string;parse_status:string;
  minimum_grade:number|null;allowed_department_ids:string[];fingerprint:string;created_at:string;updated_at:string;document_id:string|null;
  can_preview:boolean;can_delete:boolean;can_remove_record:boolean;can_withdraw:boolean;can_review:boolean;can_retry:boolean;error_json?:{message:string}|null;
}
interface Preview {format:string;page_count:number;text:string;pages?:{page:number;text:string}[];blocks?:{type:string;text?:string;style?:string;rows?:string[][]}[];}
interface Detail extends Entry {reviews:{review_id:string;decision:string;comment:string;created_at:string}[];jobs:{job_id:string;job_type:string;status:string;error_json?:{message:string};updated_at:string}[];}
interface Options {departments:{department_id:string;name:string}[];max_files:number;max_file_bytes:number;}
interface UploadResult {filename:string;success:boolean;error?:{message:string};application?:Entry;}
type Category='effective'|'review'|'processing'|'progress'|'failed'|'history';
interface Filters {scope:'mine'|'all';category:Category;q:string;format:string;department:string;page:number;limit:number;}
interface Catalog {items:Entry[];total:number;counts:Partial<Record<Category,number>>;page:number;limit:number;}
type Maintenance='delete'|'remove-record'|'withdraw';
const statuses:Record<string,string>={pending_review:'待审核',approved_pending_ingest:'批准待处理',parsing:'解析中',pending_index:'待索引',vectorizing:'向量化中',indexing:'索引中',retrievable:'可检索',rejected:'已驳回',failed:'处理失败',withdrawn:'已撤回',deleting:'清理中',invalidated:'已失效'};
const categoryLabels:Record<Category,string>={effective:'生效中',review:'待审核',processing:'处理中',progress:'提交进度',failed:'处理失败',history:'历史记录'};
const jobStatuses:Record<string,string>={pending:'排队中',running:'处理中',completed:'已完成',failed:'失败',cancelled:'已取消'};

/** 接收服务端身份，展示按权限隔离的分页知识目录、上传与详情；管理员审核和成员提交进度分别呈现。 */
export function KnowledgePage({user}: {user:UserProfile}) {
  // 页面范围只取服务端身份；成员固定本人提交，管理员才有全库与审核分类。
  const admin=user.role==='admin';
  const [filters,setFilters]=useState<Filters>({scope:admin?'all':'mine',category:'effective',q:'',format:'',department:'',page:1,limit:10});
  const [search,setSearch]=useState('');
  const [catalog,setCatalog]=useState<Catalog>({items:[],total:0,counts:{},page:1,limit:10});
  const [options,setOptions]=useState<Options|null>(null);
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [drawer,setDrawer]=useState<'upload'|'detail'|null>(null);
  const [selected,setSelected]=useState<Detail|null>(null);
  const [detailLoading,setDetailLoading]=useState(false);
  const [detailTab,setDetailTab]=useState<'info'|'history'>('info');
  const [files,setFiles]=useState<File[]>([]);
  const [departments,setDepartments]=useState<string[]>([]);
  const [grade,setGrade]=useState('');
  const [uploading,setUploading]=useState(false);
  const [results,setResults]=useState<UploadResult[]>([]);
  const [actionBusy,setActionBusy]=useState(false);
  const [comment,setComment]=useState('');
  const [preview,setPreview]=useState<Preview|null>(null);
  const [previewBusy,setPreviewBusy]=useState(false);
  const [previewError,setPreviewError]=useState('');
  const [pdfPage,setPdfPage]=useState(1);
  const [zoom,setZoom]=useState(100);
  const [image,setImage]=useState('');
  const [confirmation,setConfirmation]=useState<{action:Maintenance;entry:Entry}|null>(null);
  const [discardUpload,setDiscardUpload]=useState(false);
  // 请求序号阻止快速切换范围或文件后，旧响应覆盖新页面或串到另一份预览。
  const catalogSequence=useRef(0),detailSequence=useRef(0),previewSequence=useRef(0);
  const uploadInput=useRef<HTMLInputElement>(null);
  const tabs:Category[]=admin?['effective','review','processing','failed','history']:['effective','progress','failed','history'];

  /** 接收局部筛选值并重置页码；变更搜索范围时清空旧页，避免混淆当前筛选与旧结果。 */
  function changeFilters(next:Partial<Filters>) {setCatalog(value=>({...value,items:[],total:0}));setFilters(value=>({...value,...next,page:next.page??1}));}

  /** 无参数关闭面板并失效未返回的详情/预览；上传未发送文件先提示，处理中不允许关闭。 */
  function closeDrawer() {
    if(uploading||actionBusy)return;
    if(drawer==='upload'&&files.length){setDiscardUpload(true);return;}
    detailSequence.current++;previewSequence.current++;setDrawer(null);setSelected(null);setPreview(null);setPreviewError('');
  }

  /** 接收是否静默刷新，按当前范围获取服务端筛选、统计和分页结果；晚到请求不更新状态。 */
  const reload=useCallback(async(quiet=false)=>{
    const sequence=++catalogSequence.current;
    if(!quiet)setLoading(true);
    const query=new URLSearchParams({scope:admin?filters.scope:'mine',category:!admin&&['review','processing'].includes(filters.category)?'progress':filters.category,q:filters.q,file_format:filters.format,page:String(filters.page),limit:String(filters.limit)});
    if(filters.department)query.set('department_id',filters.department);
    try {
      const data=await apiRequest<Catalog>(`/knowledge/applications/catalog?${query}`);
      if(sequence!==catalogSequence.current)return;
      setCatalog(data);setError('');
      if(data.page!==filters.page)setFilters(value=>({...value,page:data.page}));
      setSelected(value=>{const updated=data.items.find(row=>row.application_id===value?.application_id);return value&&updated?{...value,...updated}:value;});
    } catch(reason){if(sequence===catalogSequence.current)setError(reason instanceof Error?reason.message:'读取材料失败');}
    finally{if(sequence===catalogSequence.current)setLoading(false);}
  },[filters,admin]);

  useEffect(()=>{void reload();const timer=setInterval(()=>void reload(true),5000);return()=>{catalogSequence.current++;clearInterval(timer);};},[reload]);
  useEffect(()=>{const timer=setTimeout(()=>{if(search!==filters.q)changeFilters({q:search});},300);return()=>clearTimeout(timer);},[search,filters.q]);
  useEffect(()=>{
    let active=true;
    if(user.can_use_knowledge)apiRequest<Options>('/knowledge/options').then(value=>{if(active)setOptions(value);}).catch(()=>{if(active)setOptions(null);});
    else setOptions(null);
    return()=>{active=false;};
  },[user.permission_version,user.can_use_knowledge]);
  useEffect(()=>{
    detailSequence.current++;previewSequence.current++;setSelected(null);setDrawer(null);setPreview(null);setConfirmation(null);
    if(!admin)setFilters(value=>({...value,scope:'mine',category:'effective',page:1}));
  },[admin,user.permission_version]);
  useEffect(()=>{if(selected&&!selected.can_preview){previewSequence.current++;setPreview(null);setImage('');}},[selected?.can_preview]);

  useEffect(()=>{
    if(!selected||!preview||preview.format!=='pdf'){setImage('');return;}
    const controller=new AbortController();let objectUrl='';setImage('');
    knowledgeBlob(`/knowledge/applications/${selected.application_id}/pages/${pdfPage}`,controller.signal).then(blob=>{if(!controller.signal.aborted){objectUrl=URL.createObjectURL(blob);setImage(objectUrl);}}).catch(reason=>{if(!controller.signal.aborted)setPreviewError(reason instanceof Error?reason.message:'PDF 页面读取失败');});
    return()=>{controller.abort();if(objectUrl)URL.revokeObjectURL(objectUrl);};
  },[selected?.application_id,preview,pdfPage]);

  /** 接收选中文件，打开管理详情并加载真实处理记录；清空旧文件的正文和操作错误。 */
  async function select(entry:Entry) {
    const sequence=++detailSequence.current;previewSequence.current++;
    setDrawer('detail');setSelected({...entry,reviews:[],jobs:[]});setDetailTab('info');setPreview(null);setPreviewError('');setPreviewBusy(false);setComment('');setDetailLoading(true);setError('');
    try{const data=await apiRequest<Detail>(`/knowledge/applications/${entry.application_id}`);if(sequence===detailSequence.current)setSelected(data);}
    catch(reason){if(sequence===detailSequence.current){setSelected(null);setError(reason instanceof Error?reason.message:'无法读取材料');}}
    finally{if(sequence===detailSequence.current)setDetailLoading(false);}
  }

  /** 接收上传表单，逐文件提交现有ACL；成功切换到对应处理分类，失败文件保留供再次提交。 */
  async function submit(event:FormEvent) {
    event.preventDefault();if(uploading||!files.length||!options)return;
    if(files.length>options.max_files||files.some(file=>file.size>options.max_file_bytes)){setError('文件数量或单个文件大小超过上传限制');return;}
    setUploading(true);setError('');setNotice('');setResults([]);
    const body=new FormData();body.set('metadata',JSON.stringify({allowed_department_ids:departments,minimum_grade:grade?Number(grade):null}));files.forEach(file=>body.append('files',file));
    try{
      const data=await uploadKnowledge<{results:UploadResult[]}>(body);setResults(data.results);
      const succeeded=data.results.filter(row=>row.success).length;setNotice(`已提交 ${succeeded} 份，失败 ${data.results.length-succeeded} 份`);
      setFiles(current=>current.filter((_,index)=>!data.results[index]?.success));if(uploadInput.current)uploadInput.current.value='';
      if(succeeded)changeFilters({scope:admin?filters.scope:'mine',category:admin?'processing':'progress'});
      if(data.results.every(row=>row.success))setDrawer(null);
    }catch(reason){setError(reason instanceof Error?reason.message:'提交失败');}
    finally{setUploading(false);}
  }

  /** 接收维护动作及冻结的目标记录，执行停用、撤回、目录清理或重试；更新列表后重新读取仍可见的详情。 */
  async function maintain(action:Maintenance|'retry',entry:Entry) {
    if(actionBusy)return;setActionBusy(true);setError('');
    try{
      await apiRequest(`/knowledge/applications/${entry.application_id}/${action}`,'POST');setConfirmation(null);
      setNotice(action==='remove-record'?'失效记录已从管理列表移除，审计档案保留。':action==='delete'?'文件已停止访问与检索，后台正在清理。':action==='withdraw'?'申请已撤回。':'已重新排入处理任务。');
      await reload(true);
      if(action==='remove-record'){detailSequence.current++;setSelected(null);setDrawer(null);}
      else if(selected?.application_id===entry.application_id)await select(entry);
    }catch(reason){setError(reason instanceof Error?reason.message:'操作失败');}
    finally{setActionBusy(false);}
  }

  /** 接收通过/驳回，提交真实指纹及审核意见；后端校验当前管理员和解析状态。 */
  async function review(decision:'approved'|'rejected') {
    if(!selected||actionBusy)return;setActionBusy(true);setError('');
    try{await apiRequest(`/knowledge/applications/${selected.application_id}/review`,'POST',{decision,expected_fingerprint:selected.fingerprint,comment});setNotice(decision==='approved'?'已批准，后台开始处理。':'已驳回申请。');await reload(true);await select(selected);}
    catch(reason){setError(reason instanceof Error?reason.message:'审核失败');}finally{setActionBusy(false);}
  }

  /** 无参数读取当前受权文件的真实正文；请求序号避免快速切换文件时显示上一份内容。 */
  async function openPreview() {
    if(!selected)return;const sequence=++previewSequence.current;setPreviewBusy(true);setPreviewError('');setPreview(null);setPdfPage(1);setZoom(100);
    try{const data=await apiRequest<Preview>(`/knowledge/applications/${selected.application_id}/preview`);if(sequence===previewSequence.current)setPreview(data);}
    catch(reason){if(sequence===previewSequence.current)setPreviewError(reason instanceof Error?reason.message:'预览失败');}
    finally{if(sequence===previewSequence.current)setPreviewBusy(false);}
  }

  /** 无参数鉴权下载当前原件，通过临时Blob地址交付并释放；不暴露对象存储链接。 */
  async function download() {
    if(!selected)return;
    try{const blob=await knowledgeBlob(`/knowledge/applications/${selected.application_id}/download`);const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=selected.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
    catch(reason){setPreviewError(reason instanceof Error?reason.message:'下载失败');}
  }

  /** 接收部门编号数组，返回当前有权取得的名称；找不到的部门保持受限提示，不猜测正文权限。 */
  function names(ids:string[]) {return ids.length?ids.map(id=>options?.departments.find(item=>item.department_id===id)?.name||'受限部门').join('、'):'全部门';}

  return <section className={`mg-page knowledge-page-v2 ${drawer?'with-drawer':''}`}>
    <header className="mg-heading"><div><h1>知识库管理</h1><p>{admin?'管理生效文件、审核申请与历史记录。':'管理我的提交，查看文件状态与处理进度。'}</p></div><div className="mg-heading-actions"><button className="mg-button" disabled={loading} onClick={()=>void reload()}><RefreshCw size={16} className={loading?'spin':''}/>刷新</button><button className="mg-button primary" disabled={!user.can_use_knowledge||!options||uploading||actionBusy} onClick={()=>{setDrawer('upload');setResults([]);setError('');}}><Upload size={17}/>上传文件</button></div></header>
    {admin?<div className="mg-scope" aria-label="材料范围">{(['all','mine'] as const).map(scope=><button key={scope} aria-pressed={filters.scope===scope} onClick={()=>{closeDrawer();changeFilters({scope});}}>{scope==='all'?'全部材料':'我的提交'}</button>)}</div>:<div className="mg-member-scope">我的提交</div>}
    {!user.can_use_knowledge&&<p className="mg-banner">尚未获得知识库使用资格；仍可查看本人提交记录。请联系管理员分配部门和职级。</p>}
    <div className="mg-body"><div className="mg-list-panel">
      <div className="mg-tabs" role="tablist" aria-label="文件状态分类">{tabs.map(category=><button role="tab" aria-selected={filters.category===category} key={category} onClick={()=>changeFilters({category})}>{categoryLabels[category]}<span>{catalog.counts[category]??0}</span></button>)}</div>
      <div className="mg-toolbar"><label className="mg-search"><Search size={18}/><input aria-label="搜索知识文件" placeholder={admin?'搜索文件名称或提交人':'搜索文件名称'} value={search} onChange={event=>setSearch(event.target.value)}/></label><select aria-label="文件格式" value={filters.format} onChange={event=>changeFilters({format:event.target.value})}><option value="">全部格式</option>{['pdf','docx','txt','md'].map(format=><option key={format} value={format}>{format.toUpperCase()}</option>)}</select><select aria-label="访问部门筛选" value={filters.department} onChange={event=>changeFilters({department:event.target.value})}><option value="">全部部门</option>{options?.departments.map(item=><option key={item.department_id} value={item.department_id}>{item.name}</option>)}</select><button className="mg-text-button" onClick={()=>{setSearch('');changeFilters({q:'',format:'',department:''});}}><FilterX size={16}/>清空筛选</button></div>
      {notice&&<p role="status" className="mg-notice">{notice}<button aria-label="关闭操作提示" onClick={()=>setNotice('')}><X size={15}/></button></p>}
      {error&&<p role="alert" className="mg-error">{error}</p>}
      {filters.category==='history'&&<p className="mg-hint">历史记录不参与检索，可清理已失效记录。</p>}
      <div className="mg-table-scroll" aria-busy={loading}><table className="mg-table knowledge-table"><thead><tr><th>文件名称</th>{admin&&<th>提交人</th>}<th>访问范围</th><th>更新时间 <span aria-hidden="true">↓</span></th><th>操作</th></tr></thead><tbody>
        {catalog.items.map(row=><tr key={row.application_id} className={drawer==='detail'&&selected?.application_id===row.application_id?'selected':''}><td><div className="mg-file-cell"><FileBadge format={row.file_format}/><div><button className="mg-file-name" title={row.filename} onClick={()=>void select(row)}>{row.filename}</button><div className="mg-file-subline"><span className={`mg-status ${row.status}`}>{statuses[row.status]||row.status}</span>{row.status==='pending_review'&&<span>{row.parse_status==='completed'?'预览已就绪':row.parse_status==='failed'?'预览解析失败':'预览处理中'}</span>}</div></div></div></td>{admin&&<td>{row.creator_name||'—'}</td>}<td><span>{names(row.allowed_department_ids)}</span>{row.minimum_grade!==null&&<small>职级 ≥ {row.minimum_grade}</small>}</td><td className="mg-date">{beijingTime(row.updated_at)}</td><td><div className="mg-row-actions"><button className="mg-text-button" onClick={()=>void select(row)}>{filters.category==='history'?'查看记录':'查看'}</button>{row.can_remove_record?<button className="mg-text-button danger" disabled={actionBusy} onClick={()=>setConfirmation({action:'remove-record',entry:row})}>删除记录</button>:(row.can_delete||row.can_retry)&&<details className="mg-row-menu"><summary aria-label={`更多操作 ${row.filename}`}><MoreHorizontal size={18}/></summary><div>{row.can_retry&&<button disabled={actionBusy} onClick={()=>void maintain('retry',row)}>重试处理</button>}{row.can_delete&&<button className="danger" disabled={actionBusy} onClick={()=>setConfirmation({action:'delete',entry:row})}>停用文件</button>}</div></details>}</div></td></tr>)}
        {!catalog.items.length&&<tr><td colSpan={admin?5:4}><div className="mg-empty">{loading?<><LoaderCircle className="spin" size={25}/><p>正在加载材料…</p></>:<><FileText size={30}/><strong>{filters.q||filters.format||filters.department?'没有匹配的文件':`暂无${categoryLabels[filters.category]}文件`}</strong><p>{filters.q||filters.format||filters.department?'试试更换关键词或清空筛选条件。':'上传文件后，可在对应分类查看处理状态。'}</p></>}</div></td></tr>}
      </tbody></table></div><ManagementPagination total={catalog.total} page={catalog.page} limit={filters.limit} busy={loading} onPage={page=>changeFilters({page})} onLimit={limit=>changeFilters({limit})}/>
    </div>
    {drawer==='detail'&&<ManagementDrawer title="文件详情" onClose={closeDrawer} busy={actionBusy} footer={selected&&(selected.can_delete||selected.can_remove_record||selected.can_withdraw)?<><button className="mg-button danger-outline" disabled={actionBusy||detailLoading} onClick={()=>setConfirmation({action:selected.can_remove_record?'remove-record':selected.can_withdraw?'withdraw':'delete',entry:selected})}>{selected.can_remove_record?<Trash2 size={17}/>:<Ban size={17}/>} {selected.can_remove_record?'删除记录':selected.can_withdraw?'撤回申请':'停用文件'}</button><p>{selected.can_remove_record?'从管理列表移除，审计档案保留。':'停用或撤回后不再参与检索。'}</p></>:undefined}>
      {detailLoading&&<p role="status" className="mg-hint">正在读取文件详情…</p>}{selected&&<><div className="mg-document-title"><FileBadge format={selected.file_format}/><div><h3>{selected.filename}</h3><span className={`mg-status ${selected.status}`}>{statuses[selected.status]}</span></div></div><dl className="mg-metadata"><dt>提交人</dt><dd>{selected.creator_name||(selected.creator_user_id===user.user_id?user.display_name:selected.creator_user_id)}</dd><dt>访问范围</dt><dd>{names(selected.allowed_department_ids)}</dd><dt>最低职级</dt><dd>{selected.minimum_grade??'不限'}</dd><dt>更新时间</dt><dd>{beijingTime(selected.updated_at)}</dd></dl>
      <div className="mg-tabs compact"><button aria-pressed={detailTab==='info'} onClick={()=>setDetailTab('info')}>基本信息</button><button aria-pressed={detailTab==='history'} onClick={()=>setDetailTab('history')}>处理记录</button></div>
      {detailTab==='info'?<>{!selected.can_preview&&<p className="mg-hint">当前权限或文件状态不允许读取正文；管理记录仍可查看。</p>}<div className="mg-preview-actions"><button className="mg-button" disabled={!selected.can_preview||previewBusy||detailLoading} onClick={()=>void openPreview()}><Eye size={16}/>{previewBusy?'读取中…':'预览正文'}</button><button className="mg-button" disabled={!selected.can_preview||detailLoading} onClick={()=>void download()}><Download size={16}/>下载原件</button></div>
      {selected.error_json&&<p className="mg-error">{selected.error_json.message}</p>}{selected.can_retry&&<button className="mg-button" disabled={actionBusy} onClick={()=>void maintain('retry',selected)}>重试处理</button>}
      {admin&&selected.can_review&&<section className="mg-review"><h3>审核申请</h3><label>审核意见<textarea aria-label="审核意见" maxLength={2000} value={comment} onChange={event=>setComment(event.target.value)} disabled={actionBusy}/></label><p>按当前申请的访问范围审核，不修改原申请权限。</p><div><button className="mg-button primary" title={selected.parse_status!=='completed'?'正文尚未解析完成，暂不能批准':undefined} disabled={actionBusy||detailLoading||selected.parse_status!=='completed'} onClick={()=>void review('approved')}>通过</button><button className="mg-button" disabled={actionBusy||detailLoading} onClick={()=>void review('rejected')}>驳回</button></div></section>}
      {previewError&&<p role="alert" className="mg-error">{previewError}</p>}{preview&&<div className="mg-document-preview">{preview.format==='pdf'?<><div className="mg-pdf-toolbar"><button aria-label="上一页PDF" disabled={pdfPage<=1} onClick={()=>{setPreviewError('');setPdfPage(value=>value-1);}}><ChevronLeft size={17}/></button><span>{pdfPage} / {preview.page_count}</span><button aria-label="下一页PDF" disabled={pdfPage>=preview.page_count} onClick={()=>{setPreviewError('');setPdfPage(value=>value+1);}}><ChevronRight size={17}/></button><button aria-label="缩小PDF" disabled={zoom<=50} onClick={()=>setZoom(value=>value-25)}>−</button><span>{zoom}%</span><button aria-label="放大PDF" disabled={zoom>=250} onClick={()=>setZoom(value=>value+25)}>+</button></div>{image?<div className="pdf-image-scroll"><img style={{width:`${zoom}%`}} src={image} alt={`PDF 第 ${pdfPage} 页`}/></div>:!previewError&&<p>正在读取页面…</p>}<details><summary>本页文字</summary><pre>{preview.pages?.find(item=>item.page===pdfPage)?.text}</pre></details></>:preview.blocks?.length?preview.blocks.map((block,index)=>block.type==='table'?<div className="mg-preview-table" key={index}><table><tbody>{block.rows?.map((row,i)=><tr key={i}>{row.map((cell,j)=><td key={j}>{cell}</td>)}</tr>)}</tbody></table></div>:<p key={index} className={block.style?.match(/Heading|标题/i)?'docx-heading':''}>{block.text}</p>):<pre>{preview.text}</pre>}</div>}</>:<div className="mg-timeline">{selected.reviews.map(item=><article key={item.review_id}><strong>{item.decision==='approved'?'审核通过':'审核驳回'}</strong><time>{beijingTime(item.created_at)}</time>{item.comment&&<p>{item.comment}</p>}</article>)}{selected.jobs.map(job=><article key={job.job_id}><strong>{{preview:'正文解析',publish:'导入与索引',delete:'停用清理'}[job.job_type]||'文件处理'} · {jobStatuses[job.status]||job.status}</strong><time>{beijingTime(job.updated_at)}</time>{job.error_json&&<p className="mg-error">{job.error_json.message}</p>}</article>)}{!selected.reviews.length&&!selected.jobs.length&&<p className="mg-hint">暂无处理记录。</p>}</div>}</>}
    </ManagementDrawer>}
    {drawer==='upload'&&<ManagementDrawer title={admin?'上传文件':'提交文件'} onClose={closeDrawer} busy={uploading} footer={<><button className="mg-button primary" type="submit" form="knowledge-upload-form" disabled={uploading||!files.length||!options}>{uploading?'正在提交…':admin?'确认并导入':'提交审核'}</button><p>{admin?'确认后按所选访问范围导入。':'审核通过并建立索引后方可检索。'}</p></>}><form id="knowledge-upload-form" className="mg-upload-form" onSubmit={submit}>
      <label className="mg-upload-zone"><Upload size={28}/><strong>点击选择文件</strong><span>PDF / DOCX / TXT / Markdown</span><input ref={uploadInput} aria-label="选择知识文件" type="file" multiple accept=".pdf,.docx,.txt,.md,.markdown" disabled={uploading} onChange={event=>{setFiles(Array.from(event.target.files||[]));setResults([]);}}/></label><p className="mg-hint">{options?`每次最多 ${options.max_files} 份，单份最多 ${Math.round(options.max_file_bytes/1024/1024)} MB。`:'正在读取上传限制…'}</p>
      <ul className="mg-upload-files">{files.map((file,index)=><li key={`${file.name}-${index}`}><FileText size={17}/><span title={file.name}>{file.name}</span><button type="button" aria-label={`移除 ${file.name}`} disabled={uploading} onClick={()=>setFiles(current=>current.filter((_,i)=>i!==index))}><X size={16}/></button></li>)}</ul>
      <h3>访问范围</h3><fieldset className="mg-department-options"><legend>允许部门</legend><p>不选择表示部门不限。</p>{options?.departments.map(item=><label key={item.department_id}><input type="checkbox" checked={departments.includes(item.department_id)} disabled={uploading} onChange={event=>setDepartments(current=>event.target.checked?[...current,item.department_id]:current.filter(id=>id!==item.department_id))}/>{item.name}</label>)}</fieldset><label className="mg-field">最低职级<select value={grade} disabled={uploading} onChange={event=>setGrade(event.target.value)}><option value="">不限</option>{Array.from({length:10},(_,i)=><option key={i+1} value={i+1}>{i+1}</option>)}</select></label>
      {!admin&&(grade&&Number(grade)>(user.grade||0)||departments.length>0&&!departments.includes(user.department_id||''))&&<p className="mg-banner">此访问范围不包含你：提交后可管理申请，但不能预览正文。</p>}
      {results.length>0&&<ul className="mg-upload-results">{results.map((item,index)=><li key={index}>{item.success?<Check size={16}/>:<X size={16}/>}<span>{item.filename}：{item.success?'已提交':item.error?.message}</span></li>)}</ul>}{error&&<p role="alert" className="mg-error">{error}</p>}
    </form></ManagementDrawer>}
    </div>
    <ManagementConfirm open={!!confirmation} title={confirmation?.action==='remove-record'?'删除失效记录？':confirmation?.action==='withdraw'?'撤回申请？':'停用文件？'} danger busy={actionBusy} confirmText={confirmation?.action==='remove-record'?'删除记录':confirmation?.action==='withdraw'?'撤回申请':'停用文件'} onCancel={()=>setConfirmation(null)} onConfirm={()=>{if(confirmation)void maintain(confirmation.action,confirmation.entry);}}><div className="mg-confirm-file"><FileBadge format={confirmation?.entry.file_format||''}/><strong>{confirmation?.entry.filename}</strong></div><p>{confirmation?.action==='remove-record'?'此文件已不参与检索。删除后将从管理列表移除，审计档案仍保留。':confirmation?.action==='withdraw'?'撤回后终止本次申请；已保存的审核和操作记录保留。':'确认后立即停止此文件的正文访问及检索，后台完成清理后进入历史记录。'}</p>{error&&<p role="alert" className="mg-error">{error}</p>}</ManagementConfirm>
    <ManagementConfirm open={discardUpload} title="放弃本次上传？" confirmText="放弃上传" onCancel={()=>setDiscardUpload(false)} onConfirm={()=>{setFiles([]);setDrawer(null);setDiscardUpload(false);}}><p>已选文件尚未提交，关闭后需要重新选择。</p></ManagementConfirm>
  </section>;
}
