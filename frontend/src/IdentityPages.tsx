import { useEffect, useState, type FormEvent } from 'react';
import { apiRequest, type UserProfile } from './api';

type Staff = Omit<UserProfile, 'can_use_knowledge' | 'can_query_business'> & { status: string };
type Department = { department_id: string; name: string };

/** 接收本人资料及刷新/退出回调；展示可信岗位、权限和基本设置，不提供记忆管理。 */
export function PersonalCenter({user,refresh,logout}: {user: UserProfile;refresh: () => Promise<void>;logout: () => Promise<void>}) {
  const [name,setName] = useState(user.display_name);
  const [busy,setBusy] = useState(false);
  const [notice,setNotice] = useState('');
  useEffect(() => { setName(user.display_name); },[user.display_name]);

  /** 接收表单提交，保存本人姓名后刷新可信资料；失败展示服务端说明。 */
  async function saveName(event: FormEvent) {
    event.preventDefault(); setBusy(true); setNotice('');
    try { await apiRequest('/profile','PATCH',{display_name:name}); await refresh(); setNotice('姓名已更新'); }
    catch (error) { setNotice(error instanceof Error ? error.message : '保存失败'); }
    finally { setBusy(false); }
  }

  /** 接收密码表单，验证并修改密码；成功后全部会话撤销，返回登录界面。 */
  async function savePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget); setBusy(true); setNotice('');
    try { await apiRequest('/profile/password','POST',{current_password:data.get('current_password'),new_password:data.get('new_password')}); await logout(); }
    catch (error) { setNotice(error instanceof Error ? error.message : '修改失败'); }
    finally { setBusy(false); }
  }

  return <section className="identity-page"><h1>个人中心</h1><p className="muted">账号资料与基本设置</p>
    <dl className="profile-details"><div><dt>账号</dt><dd>{user.username}</dd></div><div><dt>角色</dt><dd>{user.role === 'admin' ? '系统管理员' : '普通成员'}</dd></div>
      <div><dt>部门</dt><dd>{user.department_name || '未分配'}</dd></div><div><dt>职级</dt><dd>{user.grade ?? '未分配'}</dd></div><div><dt>岗位</dt><dd>{user.position || '未分配'}</dd></div>
      <div><dt>知识库</dt><dd>{user.can_use_knowledge ? '已授权' : '未授权'}</dd></div><div><dt>业务查询</dt><dd>{user.can_query_business ? '已授权' : '未授权'}</dd></div></dl>
    <form onSubmit={saveName} className="settings-form"><h2>修改姓名</h2><label htmlFor="display-name">姓名</label><input id="display-name" value={name} onChange={event => setName(event.target.value)} maxLength={80} required />
      <button className="action-button" disabled={busy}>保存姓名</button></form>
    <form onSubmit={savePassword} className="settings-form"><h2>修改密码</h2><label htmlFor="current-password">当前密码</label><input id="current-password" name="current_password" type="password" autoComplete="current-password" required />
      <label htmlFor="new-password">新密码（至少12位）</label><input id="new-password" name="new_password" type="password" autoComplete="new-password" minLength={12} maxLength={128} required />
      <p className="muted small">修改成功后需要重新登录。</p><button className="action-button" disabled={busy}>更新密码</button></form>
    <p role="status">{notice}</p><button className="secondary-button" disabled={busy} onClick={() => void logout()}>退出登录</button>
  </section>;
}

/** 接收本人资料刷新回调；管理员从真实目录选择职员、部门和职级并保存授权。 */
export function StaffManagement({refresh}: {refresh: () => Promise<void>}) {
  const [staff,setStaff] = useState<Staff[]>([]);
  const [departments,setDepartments] = useState<Department[]>([]);
  const [selected,setSelected] = useState<Staff | null>(null);
  const [department,setDepartment] = useState('');
  const [grade,setGrade] = useState('');
  const [position,setPosition] = useState('');
  const [business,setBusiness] = useState(false);
  const [busy,setBusy] = useState(false);
  const [notice,setNotice] = useState('');

  /** 无参数读取当前管理员可见职员与启用部门；失败显示说明，不使用假数据。 */
  async function load() {
    setBusy(true);
    try { const [users,items] = await Promise.all([apiRequest<Staff[]>('/admin/users'),apiRequest<Department[]>('/admin/departments')]); setStaff(users); setDepartments(items); }
    catch (error) { setNotice(error instanceof Error ? error.message : '读取失败'); }
    finally { setBusy(false); }
  }
  useEffect(() => { void load(); },[]);
  useEffect(() => {
    if (selected) { document.getElementById('staff-editor')?.scrollIntoView({block:'nearest'}); document.getElementById('staff-department')?.focus(); }
  },[selected]);

  /** 接收目录中的职员，填入真实现有字段并打开单条编辑区；不改变可信身份。 */
  function select(user: Staff) { setSelected(user); setDepartment(user.department_id || ''); setGrade(user.grade?.toString() || ''); setPosition(user.position || ''); setBusiness(user.business_query_enabled); setNotice(''); }

  /** 接收分配表单，提交目标 ID 与当前权限版本；成功重新读目录和本人状态。 */
  async function save(event: FormEvent) {
    event.preventDefault(); if (!selected) return; setBusy(true); setNotice('');
    try { await apiRequest(`/admin/users/${selected.user_id}`,'PATCH',{department_id:department || null,grade:grade ? Number(grade) : null,position:position || null,
      business_query_enabled:business,expected_permission_version:selected.permission_version}); setSelected(null); await load(); await refresh(); setNotice('职员资料与权限已保存'); }
    catch (error) { setNotice(error instanceof Error ? error.message : '保存失败'); }
    finally { setBusy(false); }
  }

  return <section className="identity-page"><div className="identity-heading"><div><h1>职员管理</h1><p className="muted">分配部门、职级与业务查询资格</p></div><button className="secondary-button" disabled={busy} onClick={() => void load()}>刷新列表</button></div>
    <p className="muted">部门和职级同时分配后获得知识使用资格。业务查询还需单独开启，数据范围将由后端策略限制。</p>
    <div className="staff-list">{staff.map(user => <div className="staff-row" key={user.user_id}><div><strong>{user.display_name}</strong><span>{user.username}　{user.role === 'admin' ? '管理员' : '普通成员'}</span></div>
      <div>{user.department_name || '未分配部门'}<span>职级：{user.grade ?? '未分配'}</span></div><div>知识：{user.knowledge_enabled || user.role === 'admin' ? '已授权' : '未授权'}<span>业务：{user.business_query_enabled ? '已开启' : '未开启'}</span></div>
      <button className="secondary-button" disabled={busy} aria-label={`管理 ${user.username}`} onClick={() => select(user)}>管理</button></div>)}</div>
    {selected && <form id="staff-editor" className="staff-editor" onSubmit={save}><h2>管理 {selected.display_name}（{selected.username}）</h2><div className="staff-fields">
      <label>部门<select id="staff-department" value={department} onChange={event => { setDepartment(event.target.value); if (!event.target.value) { setGrade(''); setBusiness(false); } }}><option value="">未分配</option>{departments.map(item => <option key={item.department_id} value={item.department_id}>{item.name}</option>)}</select></label>
      <label>职级<select id="staff-grade" value={grade} onChange={event => { setGrade(event.target.value); if (!event.target.value) { setDepartment(''); setBusiness(false); } }}><option value="">未分配</option>{Array.from({length:10},(_,index) => <option key={index+1} value={index+1}>{index+1}</option>)}</select></label>
      <label>岗位<input id="staff-position" value={position} onChange={event => setPosition(event.target.value)} maxLength={80} /></label></div>
      <label className="checkbox-label"><input id="staff-business" type="checkbox" checked={business} disabled={!department || !grade} onChange={event => setBusiness(event.target.checked)} />允许业务数据库查询</label>
      <div className="form-actions"><button className="action-button" disabled={busy}>{busy ? '保存中…' : '保存分配'}</button><button type="button" className="secondary-button" onClick={() => setSelected(null)}>取消</button></div></form>}
    <p role="status" className="identity-notice">{notice}</p>
  </section>;
}
