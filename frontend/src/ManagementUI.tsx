import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { ChevronLeft, ChevronRight, FileText, X } from 'lucide-react';

/** 接收文件格式，返回统一格式图标；格式文字与色彩同时提示类型，不依赖颜色识别。 */
export function FileBadge({format}: {format: string}) {
  return <span className={`mg-file-badge ${format}`} aria-hidden="true">{format==='docx' ? 'W' : format==='pdf' ? 'PDF' : format==='md' ? 'M↓' : <FileText size={19}/>}</span>;
}

/** 接收真实总数、页码、页容量和切页回调，返回固定位置分页；空列表不制造第二页。 */
export function ManagementPagination({total,page,limit,busy,onPage,onLimit,label='条'}: {total:number;page:number;limit:number;busy:boolean;onPage:(page:number)=>void;onLimit:(limit:number)=>void;label?:string}) {
  const pages=Math.max(1,Math.ceil(total/limit));
  // 分页只展示当前附近页码，避免大量数据时按钮挤占整个页脚。
  const start=Math.max(1,Math.min(page-1,pages-4));
  const numbers=Array.from({length:Math.min(5,pages)},(_,index)=>start+index);
  return <footer className="mg-pagination"><div>共 {total} {label}<span className="mg-page-size"> · 每页 <select aria-label="每页条数" value={limit} disabled={busy} onChange={event=>onLimit(Number(event.target.value))}>{[10,20,50].map(value=><option key={value} value={value}>{value}</option>)}</select> 条</span></div>
    <nav aria-label="列表分页"><button aria-label="上一页" disabled={busy||page<=1} onClick={()=>onPage(page-1)}><ChevronLeft size={17}/></button>{start>1&&<span>…</span>}{numbers.map(value=><button key={value} aria-label={`第 ${value} 页`} aria-current={value===page ? 'page' : undefined} disabled={busy} onClick={()=>onPage(value)}>{value}</button>)}{start+numbers.length-1<pages&&<span>…</span>}<button aria-label="下一页" disabled={busy||page>=pages} onClick={()=>onPage(page+1)}><ChevronRight size={17}/></button></nav>
  </footer>;
}

/** 接收标题、内容及关闭动作，返回右侧面板；首次打开定位标题，Escape交给调用者处理未保存内容。 */
export function ManagementDrawer({title,onClose,children,footer,busy=false}: {title:string;onClose:()=>void;children:ReactNode;footer?:ReactNode;busy?:boolean}) {
  const heading=useRef<HTMLHeadingElement>(null);
  const id=useId();
  useEffect(()=>{heading.current?.focus();},[]);
  return <aside className="mg-drawer" role="dialog" aria-labelledby={id} onKeyDown={event=>{if(event.key==='Escape'&&!busy){event.stopPropagation();onClose();}}}>
    <header><h2 id={id} ref={heading} tabIndex={-1}>{title}</h2><button className="mg-icon-button" disabled={busy} aria-label={`关闭${title}`} onClick={onClose}><X size={20}/></button></header>
    <div className="mg-drawer-content">{children}</div>{footer&&<footer className="mg-drawer-footer">{footer}</footer>}
  </aside>;
}

/** 接收确认动作及说明，通过Portal在整个视口正中心弹窗；浏览器管理模态焦点和背景禁用，默认聚焦取消。 */
export function ManagementConfirm({open,title,children,onCancel,onConfirm,confirmText='确认',danger=false,busy=false}: {open:boolean;title:string;children:ReactNode;onCancel:()=>void;onConfirm:()=>void;confirmText?:string;danger?:boolean;busy?:boolean}) {
  const dialog=useRef<HTMLDialogElement>(null);
  const cancel=useRef<HTMLButtonElement>(null);
  const id=useId();
  useEffect(()=>{
    if(!open)return;
    const previous=document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const node=dialog.current;
    node?.showModal();cancel.current?.focus();
    return()=>{node?.close();if(previous?.isConnected)previous.focus();};
  },[open]);
  if(!open)return null;
  return createPortal(<dialog className="mg-confirm" ref={dialog} aria-labelledby={id} onCancel={event=>{event.preventDefault();if(!busy)onCancel();}}>
    <header><h2 id={id}>{title}</h2><button aria-label="关闭确认窗口" className="mg-icon-button" disabled={busy} onClick={onCancel}><X size={19}/></button></header>
    <div className="mg-confirm-body">{children}</div><footer><button ref={cancel} className="mg-button" disabled={busy} onClick={onCancel}>取消</button><button className={danger ? 'mg-button danger-solid' : 'mg-button primary'} disabled={busy} onClick={onConfirm}>{busy ? '处理中…' : confirmText}</button></footer>
  </dialog>,document.body);
}
