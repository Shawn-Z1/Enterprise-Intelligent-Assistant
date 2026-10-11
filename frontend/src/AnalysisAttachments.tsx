import {useEffect,useState,type ChangeEvent} from 'react';
import {apiRequest,uploadAnalysis,type AnalysisAttachment,type Conversation} from './api';

/** 接收当前会话和回调，上传本人CSV/XLSX并选择输入；刷新从服务端恢复，不在浏览器解析正文。 */
export function AnalysisAttachments({conversationId,disabled,select,changed,selected,uploading}:{conversationId:string|null;disabled:boolean;select:(id:string)=>void;changed:()=>Promise<void>;selected:(ids:string[])=>void;uploading:(value:boolean)=>void}){
  const [files,setFiles]=useState<AnalysisAttachment[]>([]);
  const [ids,setIds]=useState<string[]>([]);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  useEffect(()=>{
    let alive=true;setFiles([]);setIds([]);selected([]);setError('');
    if(conversationId)void apiRequest<AnalysisAttachment[]>(`/conversations/${conversationId}/attachments`).then(rows=>{
      if(alive){setFiles(rows);const next=rows.slice(-10).map(file=>file.attachment_id);setIds(next);selected(next);}
    }).catch(reason=>{if(alive)setError(reason.message);});
    return()=>{alive=false;};
  },[conversationId,selected]);
  /** 接收文件选择，必要时创建本人会话，再上传原字节；上传期间禁止重复提交。 */
  async function upload(event:ChangeEvent<HTMLInputElement>){
    const incoming=Array.from(event.target.files || []);event.target.value='';if(!incoming.length)return;
    setBusy(true);uploading(true);setError('');
    try{
      let id=conversationId;
      if(!id){const row=await apiRequest<Conversation>('/conversations','POST',{});id=row.conversation_id;}
      const form=new FormData();incoming.forEach(file=>form.append('files',file));
      const added=await uploadAnalysis(id,form);
      if(id!==conversationId){select(id);await changed();}
      else{setFiles(previous=>[...previous,...added]);const next=[...ids,...added.map(file=>file.attachment_id)].slice(-10);setIds(next);selected(next);}
    }catch(reason){setError(reason instanceof Error ? reason.message : '附件上传失败');}
    finally{setBusy(false);uploading(false);}
  }
  /** 接收编号与勾选状态，仅更新输入集合；取消勾选不删除持久原件。 */
  function choose(id:string,checked:boolean){const next=checked ? [...ids,id] : ids.filter(value=>value!==id);if(next.length>10){setError('本次最多选择10个附件');return;}setIds(next);selected(next);}
  return <div className="analysis-attachments"><label className="analysis-upload">{busy ? '正在上传…' : '上传分析文件'}<input aria-label="上传CSV或XLSX分析文件" type="file" accept=".csv,.xlsx" multiple disabled={disabled || busy} onChange={event=>void upload(event)}/></label>
    <span className="muted">CSV / XLSX · 单文件最多20MiB · 执行代码前需确认</span>
    {!!files.length && <div className="analysis-file-list">{files.map(file=><label key={file.attachment_id}><input type="checkbox" checked={ids.includes(file.attachment_id)} disabled={disabled || busy} onChange={event=>choose(file.attachment_id,event.target.checked)}/><span title={file.filename}>{file.filename}</span><small>{Math.ceil(file.size_bytes/1024)} KiB</small></label>)}</div>}
    {error && <p role="alert" className="error-text">{error}</p>}
  </div>;
}
