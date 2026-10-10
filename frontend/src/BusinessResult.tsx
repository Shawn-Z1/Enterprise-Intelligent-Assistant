import { useEffect, useState } from 'react';
import { apiRequest, type BusinessResultData } from './api';

/** 接收真实业务结果ID，复核权限后展示表格和SVG图表；失败清空数据，不重新执行SQL。 */
export function BusinessResult({resultId,close}:{resultId:string;close:()=>void}){
  const [result,setResult]=useState<BusinessResultData|null>(null);
  const [error,setError]=useState('');
  useEffect(()=>{
    let alive=true;
    /** 无参数读取本人已持久结果，撤权时清空已展示的数据。 */
    async function read(){try{const data=await apiRequest<BusinessResultData>(`/business/results/${resultId}`);if(alive){setResult(data);setError('');}}
      catch(reason){if(alive){setResult(null);setError(reason instanceof Error ? reason.message : '结果不可读取');}}}
    void read();const timer=setInterval(()=>void read(),3000);
    return()=>{alive=false;clearInterval(timer);};
  },[resultId]);
  return <aside className="source-panel business-result" aria-label="业务查询结果"><div className="source-heading"><h2>业务查询结果</h2><button aria-label="关闭业务结果" onClick={close}>关闭</button></div>
    {error && <p className="error-text" role="alert">{error}</p>}{!result && !error && <p role="status">正在核验结果权限…</p>}
    {result && <><p>读取权限：{result.scope}</p><p>返回 {result.row_count} 行{result.truncated ? '（达到200行上限，已截断）' : ''}</p>
      {result.chart && <ResultChart chart={result.chart}/>}
      <div className="business-table"><table><thead><tr>{result.columns.map(column=><th key={column}>{column}</th>)}</tr></thead><tbody>{result.rows.map((row,index)=><tr key={index}>{result.columns.map(column=><td key={column}>{row[column]===null ? '—' : String(row[column])}</td>)}</tr>)}</tbody></table></div>
      {!result.row_count && <p>当前允许范围内没有匹配记录。</p>}
    </>}
  </aside>;
}

/** 接收后端校验的真实点集，返回可访问的柱/线图；不执行生成脚本，不编造数值。 */
function ResultChart({chart}:{chart:NonNullable<BusinessResultData['chart']>}){
  const {points}=chart;if(!points.length)return null;
  const min=Math.min(0,...points.map(point=>point.value));
  const max=Math.max(0,...points.map(point=>point.value));
  const span=max-min || 1;
  const y=(value:number)=>190-(value-min)/span*150;
  const step=400/points.length;
  // 日期显示月日，长序列稀疏标注；完整日期/人员及数值仍通过每个点的title和可访问名称保留。
  const tickStep=Math.max(1,Math.ceil(points.length/6));
  const label=(value:string)=>/^\d{4}-\d{2}-\d{2}/.test(value) ? value.slice(5,10) : value.slice(0,8);
  return <figure className="business-chart"><figcaption>{chart.title}{chart.unit ? `（${chart.unit}）` : ''}</figcaption>
    <svg viewBox="0 0 460 235" role="img" aria-label={points.map(point=>`${point.label}：${point.value}`).join('；')}>
      <line x1="40" x2="440" y1={y(0)} y2={y(0)} stroke="#a5b2c5"/>
      {chart.type==='line' && <polyline points={points.map((point,index)=>`${40+step*(index+.5)},${y(point.value)}`).join(' ')} fill="none" stroke="#365cff" strokeWidth="2"/>}
      {points.map((point,index)=>{const tick=index%tickStep===0 || index===points.length-1;return <g key={index}><title>{point.label}：{point.value}</title>{chart.type==='bar' ? <rect x={40+index*step+step*.18} y={Math.min(y(0),y(point.value))} width={step*.64} height={Math.max(1,Math.abs(y(0)-y(point.value)))} fill="#4865df"/> : <circle cx={40+step*(index+.5)} cy={y(point.value)} r="3" fill="#4865df"/>}{tick && <><text x={40+step*(index+.5)} y={Math.max(20,y(point.value)-8)} textAnchor="middle" fontSize="12">{point.value}</text><text x={40+step*(index+.5)} y="220" textAnchor="middle" fontSize="12">{label(point.label)}</text></>}</g>;})}
    </svg>{chart.time_range && <p>时间范围：{chart.time_range}</p>}{chart.partial && <p>图表仅展示已返回结果中的前20行，不能当作全量统计。</p>}
  </figure>;
}
