import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, Download, X } from 'lucide-react';
import { apiRequest, knowledgeBlob, citationPages, type Citation } from './api';

interface Source extends Citation { content: string; file_format: string; }
interface Preview { format: string; page_count: number; pages?: {page:number;text:string}[]; blocks?: {type:string;text?:string;rows?:string[][]}[]; }

/** 接收引用及关闭动作，在右侧真实定位当前授权来源；失效或撤权清空正文，移动屏采用覆盖面板。 */
export function SourcePanel({citation,close}: {citation:Citation;close:()=>void}) {
  const [source,setSource]=useState<Source | null>(null);
  const [preview,setPreview]=useState<Preview | null>(null);
  const [page,setPage]=useState(citation.page || 1);
  const [image,setImage]=useState('');
  const [zoom,setZoom]=useState(100);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const button=useRef<HTMLButtonElement>(null);

  useEffect(()=>{
    const previous=document.activeElement as HTMLElement | null;
    button.current?.focus();
    const escape=(event:KeyboardEvent)=>{if(event.key==='Escape')close();};
    window.addEventListener('keydown',escape);
    return()=>{window.removeEventListener('keydown',escape);previous?.focus();};
  },[close]);

  useEffect(()=>{
    let alive=true;
    setSource(null);setPreview(null);setImage('');setError('');setPage(citation.page || 1);setZoom(100);
    /** 无参数复核引用，返回最新合法来源；定期撤权检查失败时清空已加载正文和原件。 */
    async function read(){
      try{const value=await apiRequest<Source>(`/knowledge/sources/${citation.chunk_id}`);if(alive){setSource(value);setError('');}}
      catch(reason){if(alive){setSource(null);setPreview(null);setImage('');setError(reason instanceof Error ? reason.message : '来源不可用');}}
    }
    void read();const timer=setInterval(()=>void read(),2500);
    return()=>{alive=false;clearInterval(timer);};
  },[citation.chunk_id]);

  useEffect(()=>{
    if(!source || !preview || preview.format!=='pdf'){setImage('');return;}
    const controller=new AbortController();let url='';
    setImage('');
    knowledgeBlob(`/knowledge/documents/${source.document_id}/pages/${page}`,controller.signal).then(blob=>{
      if(!controller.signal.aborted){url=URL.createObjectURL(blob);setImage(url);}
    }).catch(reason=>{if(!controller.signal.aborted){setPreview(null);setError(reason.message);}});
    return()=>{controller.abort();if(url)URL.revokeObjectURL(url);};
  },[source?.document_id,preview,page]);

  /** 无参数取得正式原件的真实预览；当前来源读权限独立于聊天与创建者身份。 */
  async function openPreview(){
    if(!source)return;setBusy(true);setError('');
    try{setPreview(await apiRequest<Preview>(`/knowledge/documents/${source.document_id}/preview`));}
    catch(reason){setError(reason instanceof Error ? reason.message : '原件读取失败');}
    finally{setBusy(false);}
  }

  /** 无参数从受鉴权正式来源接口下载原件；临时 Blob 完成后释放，不暴露 MinIO 直链。 */
  async function download(){
    if(!source)return;
    try{const blob=await knowledgeBlob(`/knowledge/documents/${source.document_id}/download`);const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=source.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
    catch(reason){setError(reason instanceof Error ? reason.message : '原件下载失败');}
  }

  return <aside className="source-panel" aria-label="知识来源"><div className="source-heading"><h2>知识来源</h2><button ref={button} aria-label="关闭知识来源" onClick={close}><X size={20}/></button></div>
    {error && <p role="alert" className="error-text">{error}</p>}
    {!source && !error && <p role="status">正在核验来源权限…</p>}
    {source && <><h3>{source.filename}</h3><p className="source-location">{citationPages(source) || source.heading || `片段 ${source.position+1}`}</p>
      <div className="source-excerpt"><p>{source.content}</p></div><div className="source-actions"><button disabled={busy} onClick={()=>void openPreview()}>{busy ? '正在读取…' : '预览原件'}</button><button onClick={()=>void download()}><Download size={17}/>下载原件</button></div>
      {preview && <div className="knowledge-preview">{preview.format==='pdf' ? <><div className="pdf-toolbar"><button aria-label="来源上一页" disabled={page<=1} onClick={()=>setPage(value=>value-1)}><ChevronLeft size={18}/></button><span>第 {page} / {preview.page_count} 页</span><button aria-label="来源下一页" disabled={page>=preview.page_count} onClick={()=>setPage(value=>value+1)}><ChevronRight size={18}/></button><button aria-label="缩小来源" disabled={zoom<=50} onClick={()=>setZoom(value=>value-25)}>−</button><span>{zoom}%</span><button aria-label="放大来源" disabled={zoom>=250} onClick={()=>setZoom(value=>value+25)}>+</button></div>{image ? <div className="pdf-image-scroll"><img className="pdf-page-image" src={image} style={{width:`${zoom}%`}} alt={`来源第 ${page} 页`}/></div> : <p>正在读取页面…</p>}</> : preview.blocks?.map((block,index)=>block.type==='table' ? <div className="docx-table" key={index}><table><tbody>{block.rows?.map((row,i)=><tr key={i}>{row.map((value,j)=><td key={j}>{value}</td>)}</tr>)}</tbody></table></div> : <p className="parsed-text" key={index}>{block.text}</p>)}</div>}
    </>}
  </aside>;
}
