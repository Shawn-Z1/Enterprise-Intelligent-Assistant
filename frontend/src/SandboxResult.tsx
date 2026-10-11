import {useEffect,useState} from 'react';
import {apiRequest,type SandboxResultData} from './api';

/** 接收本人沙箱结果ID，展示实际检查/统计/静态PNG/下载；没有前端代码执行入口。 */
export function SandboxResult({resultId,close}:{resultId:string;close:()=>void}){
  const [result,setResult]=useState<SandboxResultData|null>(null);
  const [error,setError]=useState('');
  useEffect(()=>{
    let alive=true;
    /** 读取本人持久回执，账号失效时清空，不重新执行代码。 */
    async function read(){try{const value=await apiRequest<SandboxResultData>(`/sandbox/results/${resultId}`);if(alive){setResult(value);setError('');}}catch(reason){if(alive){setResult(null);setError(reason instanceof Error ? reason.message : '结果不可读取');}}}
    void read();const timer=setInterval(()=>void read(),3000);return()=>{alive=false;clearInterval(timer);};
  },[resultId]);
  return <aside className="source-panel sandbox-result" aria-label="文件分析结果"><div className="source-heading"><h2>文件分析结果</h2><button aria-label="关闭文件分析结果" onClick={close}>关闭</button></div>
    {error && <p role="alert" className="error-text">{error}</p>}{!result && !error && <p role="status">正在读取结果…</p>}
    {result && <><p>{result.description || result.message}</p>
      {result.files?.map((file,index)=><details key={`${file.attachment_id}-${index}`}><summary>{file.file}{file.sheet ? ` / ${file.sheet}` : ''}{file.row_count!==undefined ? ` · ${file.row_count} 行` : file.text_length!==undefined ? ` · ${file.text_length} 字符` : ''}</summary>
        {file.error && <p className="error-text">{file.error}</p>}{file.table && <p>表名：{file.table}</p>}{file.columns && <p>列：{file.columns.join('、')}</p>}
        {file.headings?.length ? <p>结构：{file.headings.join('、')}</p> : null}{file.missing && <p>缺失值：{JSON.stringify(file.missing)}</p>}
        {file.sample!==undefined && <pre>{typeof file.sample==='string' ? file.sample : JSON.stringify(file.sample,null,2)}</pre>}
        {file.sample!==undefined && <p>{file.sample_complete ? '概况已覆盖可提取内容。' : '以上为有限样例，未代表全文或全表。'}</p>}
        {file.empty_pages?.length ? <p>第 {file.empty_pages.join('、')} 页没有可提取文字，本轮不做OCR。</p> : null}</details>)}
      {result.coverage && <p>读取范围：{result.coverage.scope}，{result.coverage.start}–{result.coverage.end} / {result.coverage.total}（{result.coverage.unit}）{result.has_more ? '，还有未读内容' : ''}</p>}
      {result.text && <pre className="sandbox-text-result">{result.text}</pre>}{result.stdout && <details><summary>实际执行输出</summary><pre>{result.stdout}</pre></details>}
      {result.rows && <><p>统计结果 {result.row_count} 行{result.truncated ? '，表格仅前200行' : ''}</p><div className="business-table"><table><thead><tr>{result.columns?.map(column=><th key={column}>{column}</th>)}</tr></thead><tbody>{result.rows.map((row,index)=><tr key={index}>{result.columns?.map(column=><td key={column}>{row[column]===null ? '—' : String(row[column])}</td>)}</tr>)}</tbody></table></div></>}
      {result.chart?.partial && <p>图表仅前20个结果点；完整结果以导出文件为准。</p>}
      {result.artifacts.map(artifact=><div key={artifact.artifact_id}>{artifact.filename.toLowerCase().endsWith('.png') && <img className="sandbox-chart-image" src={`/api/sandbox/artifacts/${artifact.artifact_id}`} alt={result.chart?.title || '实际计算结果图表'}/>}<a href={`/api/sandbox/artifacts/${artifact.artifact_id}`} download={artifact.filename}>下载 {artifact.filename}</a>{artifact.filename.toLowerCase().endsWith('.pdf') && <a href={`/api/sandbox/artifacts/${artifact.artifact_id}`} target="_blank" rel="noopener noreferrer">打开 PDF</a>}</div>)}
    </>}
  </aside>;
}
