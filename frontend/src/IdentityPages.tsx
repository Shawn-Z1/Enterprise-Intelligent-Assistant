import { useEffect, useState, type FormEvent } from 'react';
import { apiRequest, type UserProfile } from './api';


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
