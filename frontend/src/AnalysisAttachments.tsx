import {useEffect,useState,type ChangeEvent} from 'react';
import {FileText,X} from 'lucide-react';
import {apiRequest,uploadAnalysis,type AnalysisAttachment,type Conversation} from './api';

/** 接收会话、成功发送次数和状态回调，展示未发送文件草稿；发送后清空，已发送文件在消息中展示。 */
export function AnalysisAttachments({conversationId,disabled,select,changed,selected,uploading,sentRevision}:{conversationId:string|null;disabled:boolean;select:(id:string)=>void;changed:()=>Promise<void>;selected:(ids:string[])=>void;uploading:(value:boolean)=>void;sentRevision:number}){
  // 这里只保存本条待发送附件，历史文件由服务端会话目录提供给Agent。
  const [files,setFiles]=useState<AnalysisAttachment[]>([]);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  useEffect(()=>{
    let alive=true;setFiles([]);selected([]);setError('');
    if(conversationId)void apiRequest<AnalysisAttachment[]>(`/conversations/${conversationId}/attachments`).then(rows=>{
      if(alive){const pending=rows.filter(file=>!file.message_id);setFiles(pending);selected(pending.map(file=>file.attachment_id));}
    }).catch(reason=>{if(alive)setError(reason.message);});
    return()=>{alive=false;};
  },[conversationId,selected,sentRevision]);
  /** 接收文件选择，校验待发送总量并上传原字节；创建会话时通过回调保留已输入文字，返回草稿状态。 */
  async function upload(event:ChangeEvent<HTMLInputElement>){
    const incoming=Array.from(event.target.files || []);event.target.value='';if(!incoming.length)return;
    if(files.length+incoming.length>10 || files.reduce((sum,file)=>sum+file.size_bytes,0)+incoming.reduce((sum,file)=>sum+file.size,0)>50*1024*1024){setError('每条消息最多10个附件、合计50MiB');return;}
    setBusy(true);uploading(true);setError('');
    try{
      let id=conversationId;
      if(!id){const row=await apiRequest<Conversation>('/conversations','POST',{});id=row.conversation_id;}
      const form=new FormData();incoming.forEach(file=>form.append('files',file));
      const added=await uploadAnalysis(id,form);
      if(id!==conversationId){select(id);await changed();}
      else{const next=[...files,...added];setFiles(next);selected(next.map(file=>file.attachment_id));}
    }catch(reason){setError(reason instanceof Error ? reason.message : '附件上传失败');}
    finally{setBusy(false);uploading(false);}
  }
  /** 接收待发送附件编号，仅撤回本条草稿并更新列表；失败保留原列表，已发送文件不能在此移除。 */
  async function remove(id:string){
    if(!conversationId)return;setBusy(true);uploading(true);setError('');
    try{await apiRequest(`/conversations/${conversationId}/attachments/${id}`,'DELETE');const next=files.filter(file=>file.attachment_id!==id);setFiles(next);selected(next.map(file=>file.attachment_id));}
    catch(reason){setError(reason instanceof Error ? reason.message : '无法移除附件');}
    finally{setBusy(false);uploading(false);}
  }
  return <div className="analysis-attachments">
    {!!files.length && <div className="analysis-file-list" aria-label="本条消息待发送附件">{files.map(file=><div className="attachment-chip" key={file.attachment_id}><FileText size={18} aria-hidden="true"/><span title={file.filename}>{file.filename}</span><small>{Math.ceil(file.size_bytes/1024)} KiB</small><button type="button" aria-label={`移除待发送附件 ${file.filename}`} disabled={disabled || busy} onClick={()=>void remove(file.attachment_id)}><X size={16}/></button></div>)}</div>}
    <label className="analysis-upload">{busy ? '正在上传…' : '上传文件'}<input aria-label="上传文字、表格或文档附件" type="file" accept=".txt,.md,.csv,.xlsx,.docx,.pdf" multiple disabled={disabled || busy} onChange={event=>void upload(event)}/></label>
    <span className="muted">TXT / MD / CSV / XLSX / DOCX / PDF · 单文件最多20MiB · 发送后会话内可继续使用</span>
    {error && <p role="alert" className="error-text">{error}</p>}
  </div>;
}
