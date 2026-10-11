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
      {result.files?.map(file=><details key={`${file.attachment_id}-${file.table}`}><summary>{file.file} / {file.sheet} · {file.row_count} 行</summary><p>表名：{file.table}</p><p>列：{file.columns.join('、')}</p><p>缺失值：{JSON.stringify(file.missing)}</p><pre>{JSON.stringify(file.sample,null,2)}</pre><p>以上样例仅前3行。</p></details>)}
      {result.rows && <><p>统计结果 {result.row_count} 行{result.truncated ? '，表格仅前200行' : ''}</p><div className="business-table"><table><thead><tr>{result.columns?.map(column=><th key={column}>{column}</th>)}</tr></thead><tbody>{result.rows.map((row,index)=><tr key={index}>{result.columns?.map(column=><td key={column}>{row[column]===null ? '—' : String(row[column])}</td>)}</tr>)}</tbody></table></div></>}
      {result.chart?.partial && <p>图表仅前20个结果点；完整结果以导出文件为准。</p>}
      {result.artifacts.map(artifact=><div key={artifact.artifact_id}>{artifact.filename.endsWith('.png') && <img className="sandbox-chart-image" src={`/api/sandbox/artifacts/${artifact.artifact_id}`} alt={result.chart?.title || '实际计算结果图表'}/>}<a href={`/api/sandbox/artifacts/${artifact.artifact_id}`} download={artifact.filename}>下载 {artifact.filename}</a></div>)}
    </>}
  </aside>;
}
