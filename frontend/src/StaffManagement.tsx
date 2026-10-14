import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Info, LoaderCircle, RefreshCw, Search, UsersRound } from 'lucide-react';
import { apiRequest, type UserProfile } from './api';
import { ManagementConfirm, ManagementDrawer, ManagementPagination } from './ManagementUI';

type Staff=UserProfile&{status:string};
interface Department {department_id:string;name:string;}
interface Catalog {items:Staff[];total:number;page:number;limit:number;}

/** 接收本人资料刷新回调，展示管理员分页职员目录和右侧分配面板；不提供角色修改或账号删除。 */
export function StaffManagement({refresh}: {refresh:()=>Promise<void>}) {
  const [catalog,setCatalog]=useState<Catalog>({items:[],total:0,page:1,limit:10});
  const [departments,setDepartments]=useState<Department[]>([]);
  const [search,setSearch]=useState('');
  const [filters,setFilters]=useState({q:'',department:'',access:'all',page:1,limit:10});
  const [selected,setSelected]=useState<Staff|null>(null);
  const [department,setDepartment]=useState('');
  const [grade,setGrade]=useState('');
  const [position,setPosition]=useState('');
  const [business,setBusiness]=useState(false);
  const [loading,setLoading]=useState(true);
  const [saving,setSaving]=useState(false);
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [discard,setDiscard]=useState<{next:Staff|null}|null>(null);
  // 请求序号确保快速搜索与翻页时只接受最后一次服务端目录响应。
  const requestSequence=useRef(0);
  const dirty=!!selected&&(department!==(selected.department_id||'')||grade!==(selected.grade?.toString()||'')||position!==(selected.position||'')||business!==selected.business_query_enabled);

  /** 接收局部筛选值，清空旧页并回到第一页；所有搜索和授权过滤由服务端执行。 */
  function changeFilters(next:Partial<typeof filters>) {setCatalog(value=>({...value,items:[],total:0}));setFilters(value=>({...value,...next,page:next.page??1}));}

  /** 无参数获取当前筛选的真实职员页，丢弃过期响应；请求失败显示原因且不使用示例资料。 */
  const load=useCallback(async()=>{
    const sequence=++requestSequence.current;setLoading(true);
    const query=new URLSearchParams({q:filters.q,access:filters.access,page:String(filters.page),limit:String(filters.limit)});
    if(filters.department)query.set('department_id',filters.department);
    try{const data=await apiRequest<Catalog>(`/admin/users/catalog?${query}`);if(sequence===requestSequence.current){setCatalog(data);setError('');if(data.page!==filters.page)setFilters(value=>({...value,page:data.page}));}}
    catch(reason){if(sequence===requestSequence.current)setError(reason instanceof Error?reason.message:'读取职员列表失败');}
    finally{if(sequence===requestSequence.current)setLoading(false);}
  },[filters]);
  useEffect(()=>{void load();return()=>{requestSequence.current++;};},[load]);
  useEffect(()=>{let active=true;apiRequest<Department[]>('/admin/departments').then(value=>{if(active)setDepartments(value);}).catch(reason=>{if(active)setError(reason.message);});return()=>{active=false;};},[]);
  useEffect(()=>{const timer=setTimeout(()=>{if(search!==filters.q)changeFilters({q:search});},300);return()=>clearTimeout(timer);},[search,filters.q]);

  /** 接收目标职员或空值，打开其真实分配表单或关闭面板；这里只变更本地草稿，不保存权限。 */
  function show(user:Staff|null) {setSelected(user);setDepartment(user?.department_id||'');setGrade(user?.grade?.toString()||'');setPosition(user?.position||'');setBusiness(user?.business_query_enabled||false);setError('');}

  /** 接收下一目标，存在草稿时先居中确认放弃；保存期间阻止切换，避免更新错误的账号。 */
  function select(user:Staff|null) {if(saving)return;if(user?.user_id===selected?.user_id&&user)return;if(dirty){setDiscard({next:user});return;}show(user);}

  /** 接收保存事件，将指定职员及权限版本提交给后端；成功刷新目录和本人身份，失败保留草稿。 */
  async function save(event:FormEvent) {
    event.preventDefault();if(!selected||saving)return;setSaving(true);setError('');setNotice('');
    try{await apiRequest(`/admin/users/${selected.user_id}`,'PATCH',{department_id:department||null,grade:grade?Number(grade):null,position:position||null,business_query_enabled:business,expected_permission_version:selected.permission_version});show(null);await load();await refresh();setNotice('职员资料与权限已保存。');}
    catch(reason){setError(reason instanceof Error?reason.message:'保存失败');}finally{setSaving(false);}
  }

  return <section className={`mg-page staff-page-v2 ${selected?'with-drawer':''}`}><header className="mg-heading"><div><h1>职员管理</h1><p>管理部门、岗位与业务访问资格。</p></div><button className="mg-button" disabled={loading||saving} onClick={()=>void load()}><RefreshCw size={16} className={loading?'spin':''}/>刷新列表</button></header>
    <p className="mg-banner"><Info size={18}/>部门和职级齐全后可使用知识库；业务查询需单独开启。</p>
    <div className="mg-body"><div className="mg-list-panel"><div className="mg-toolbar"><label className="mg-search"><Search size={18}/><input aria-label="搜索职员" placeholder="搜索姓名或账号" value={search} onChange={event=>setSearch(event.target.value)}/></label><select aria-label="职员部门筛选" value={filters.department} onChange={event=>changeFilters({department:event.target.value})}><option value="">全部部门</option>{departments.map(item=><option key={item.department_id} value={item.department_id}>{item.name}</option>)}</select><select aria-label="职员授权筛选" value={filters.access} onChange={event=>changeFilters({access:event.target.value})}><option value="all">全部授权状态</option><option value="knowledge">知识库已授权</option><option value="business">业务查询已开启</option><option value="unassigned">待分配部门或职级</option></select><button className="mg-text-button" onClick={()=>{setSearch('');changeFilters({q:'',department:'',access:'all'});}}>重置</button></div>
    {notice&&<p className="mg-notice" role="status">{notice}</p>}{error&&<p className="mg-error" role="alert">{error}</p>}
    <div className="mg-table-scroll" aria-busy={loading}><table className="mg-table staff-table"><thead><tr><th>员工</th><th>部门与岗位</th><th>职级</th><th>知识库</th><th>业务查询</th><th>操作</th></tr></thead><tbody>{catalog.items.map(user=><tr key={user.user_id} className={user.user_id===selected?.user_id?'selected':''}><td><div className="mg-person"><span className="mg-person-avatar" aria-hidden="true">{user.display_name.slice(0,1)}</span><div><strong>{user.display_name}</strong><small>{user.username}{user.role==='admin'?' · 管理员':''}{user.status!=='active'?' · 已停用':''}</small></div></div></td><td>{user.department_name||'未分配'}<small>{user.position||'—'}</small></td><td>{user.grade?`L${user.grade}`:'—'}</td><td><span className={`mg-status ${user.can_use_knowledge?'retrievable':'muted'}`}>{user.can_use_knowledge?'已授权':'未授权'}</span></td><td><span className={`mg-status ${user.can_query_business?'retrievable':'muted'}`}>{user.can_query_business?'已开启':'未开启'}</span></td><td><button className="mg-text-button" aria-label={`管理 ${user.username}`} disabled={saving} onClick={()=>select(user)}>管理</button></td></tr>)}
      {!catalog.items.length&&<tr><td colSpan={6}><div className="mg-empty">{loading?<><LoaderCircle size={27} className="spin"/><p>正在加载职员…</p></>:<><UsersRound size={32}/><strong>没有匹配的职员</strong><p>请调整关键词、部门或授权条件。</p></>}</div></td></tr>}
    </tbody></table></div><ManagementPagination total={catalog.total} page={catalog.page} limit={filters.limit} busy={loading} label="名职员" onPage={page=>changeFilters({page})} onLimit={limit=>changeFilters({limit})}/></div>
    {selected&&<ManagementDrawer title="管理职员" busy={saving} onClose={()=>select(null)} footer={<><div className="mg-save-actions"><button className="mg-button" disabled={saving} onClick={()=>select(null)}>取消</button><button className="mg-button primary" type="submit" form="staff-editor" disabled={saving||!dirty}>{saving?'保存中…':'保存分配'}</button></div><p>保存后立即生效。</p></>}><div className="mg-person mg-selected-person"><span className="mg-person-avatar" aria-hidden="true">{selected.display_name.slice(0,1)}</span><div><h3>{selected.display_name}</h3><small>{selected.username}</small></div><span className="mg-status muted">{selected.role==='admin'?'管理员':'普通成员'}</span></div>
      <form id="staff-editor" className="mg-staff-form" onSubmit={save}><h3>工作信息</h3><label className="mg-field">部门<select id="staff-department" value={department} disabled={saving} onChange={event=>{setDepartment(event.target.value);if(!event.target.value){setGrade('');setBusiness(false);}}}><option value="">未分配</option>{departments.map(item=><option key={item.department_id} value={item.department_id}>{item.name}</option>)}</select></label><label className="mg-field">职级<select id="staff-grade" value={grade} disabled={saving} onChange={event=>{setGrade(event.target.value);if(!event.target.value){setDepartment('');setBusiness(false);}}}><option value="">未分配</option>{Array.from({length:10},(_,index)=><option key={index+1} value={index+1}>{index+1}</option>)}</select></label><label className="mg-field">岗位<input id="staff-position" value={position} disabled={saving} maxLength={80} onChange={event=>setPosition(event.target.value)}/></label>
      <section className="mg-access-section"><h3>访问资格</h3><p className="mg-access-line">知识库访问<span className={`mg-status ${selected.role==='admin'||department&&grade?'retrievable':'muted'}`}>{selected.role==='admin'||department&&grade?'已授权':'未授权'}</span></p><p className="mg-hint">管理员默认可用；成员根据部门与职级自动判断。</p><label className="mg-switch-label" htmlFor="staff-business"><span>允许业务数据库查询</span><input id="staff-business" type="checkbox" role="switch" checked={business} disabled={saving||!department||!grade} onChange={event=>setBusiness(event.target.checked)}/><span className="mg-switch" aria-hidden="true"/></label><p className="mg-hint">仅开放工具资格，实际数据仍按权限限制。</p>{(!department||!grade)&&<p className="mg-banner">部门或职级未分配时，业务查询不可开启。</p>}</section>
      {error&&<p className="mg-error" role="alert">{error}</p>}</form>
    </ManagementDrawer>}</div>
    <ManagementConfirm open={!!discard} title="放弃未保存的修改？" confirmText="放弃修改" onCancel={()=>setDiscard(null)} onConfirm={()=>{show(discard?.next||null);setDiscard(null);}}><p>当前职员的修改尚未保存。继续后将放弃这份草稿。</p></ManagementConfirm>
  </section>;
}
