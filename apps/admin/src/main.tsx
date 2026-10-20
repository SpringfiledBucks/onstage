import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiClient, RecordData, pageItems } from '../../../packages/api-client/index';
import '../../web/src/style.css';
const api = new ApiClient();
const message = (e: unknown) => e instanceof Error ? e.message : '请求未完成';
function Admin() {
    const [me, setMe] = useState<RecordData | null>(null), [error, setError] = useState(''), [tab, setTab] = useState('users'), [search, setSearch] = useState(''), [banned, setBanned] = useState(''), [online, setOnline] = useState(''), [userId, setUserId] = useState(''), [page, setPage] = useState(0), [cursors, setCursors] = useState<string[]>(['']), [data, setData] = useState<RecordData>({}), [busy, setBusy] = useState(false), [rev, setRev] = useState(0);
    const loadMe = () => api.bootstrap().then(d => setMe(d.user || (d.id ? d : null))).catch(e => setError(message(e)));
    useEffect(() => { loadMe(); }, []);
    useEffect(() => { if (!me?.id)
        return; const beat = () => api.request('/api/auth/heartbeat', 'POST', {}).catch(() => { }); beat(); const timer = setInterval(beat, 30000); return () => clearInterval(timer); }, [me?.id]);
    useEffect(() => { setPage(0); setCursors(['']); }, [tab, search, banned, online, userId]);
    useEffect(() => { if (!me?.admin && !me?.is_admin)
        return; let active = true; setBusy(true); const q = new URLSearchParams({ limit: '24' }); if (cursors[page])
        q.set('cursor', cursors[page]); if (tab === 'users') {
        if (search)
            q.set('search', search);
        if (banned)
            q.set('banned', banned);
        if (online)
            q.set('online', online);
    } if (tab === 'rooms' && userId)
        q.set('user_id', userId); api.request(`/api/admin/${tab}?${q}`).then(d => { if (active) {
        setData(d);
        setError('');
    } }).catch(e => { if (active)
        setError(message(e)); }).finally(() => { if (active)
        setBusy(false); }); return () => { active = false; }; }, [me, tab, search, banned, online, userId, page, rev]);
    const act = async (path: string, body: unknown) => { setBusy(true); try {
        await api.request(`/api/admin${path}`, 'POST', body);
        setRev(x => x + 1);
    }
    catch (e) {
        setError(message(e));
    }
    finally {
        setBusy(false);
    } };
    const items = pageItems(data, tab);
    return <><header className="header"><a className="brand" href="/">ONSTAGE 管理中心</a>{me && (me.admin || me.is_admin) && <><span className="account">{me.username}</span><button onClick={async () => { await api.request('/api/auth/logout', 'POST', {}); setMe(null); setData({}); }}>退出</button></>}</header><main className="layout">{error && <p className="error" role="alert">{error}</p>}{!me?.admin && !me?.is_admin ? <section className="panel"><h1>管理员登录</h1><p>此入口仅供已获管理权限的账户。普通注册不会授予管理员权限。</p><form onSubmit={async (e) => { e.preventDefault(); const f = new FormData(e.currentTarget); setBusy(true); try {
        await api.request('/api/auth/login', 'POST', { username: f.get('username'), password: f.get('password'), remember: false });
        await loadMe();
    }
    catch (e) {
        setError(message(e));
    }
    finally {
        setBusy(false);
    } }}><label>用户名<input name="username" autoComplete="username" required/></label><label>密码<input name="password" type="password" autoComplete="current-password" required/></label><button disabled={busy} className="primary">登录</button></form>{me && !me.admin && !me.is_admin && <p role="alert">当前账户没有管理权限。</p>}</section> : <><nav className="toolbar" aria-label="管理功能">{[['users', '注册用户'], ['rooms', '全部对局'], ['shares', '分享'], ['reports', '举报'], ['member-feedback', '成员赞踩'], ['feedback', '开发者反馈'], ['audit','管理审计']].map(([k, label]) => <button key={k} aria-pressed={tab === k} onClick={() => { setTab(k); setUserId(''); }}>{label}</button>)}</nav><section className="panel"><h1>{({ users: '注册用户', rooms: '对局列表', shares: '分享管理', reports: '举报处理', 'member-feedback': '私密成员评价', feedback: '开发者反馈', audit: '管理审计' } as Record<string, string>)[tab]} {data.total !== undefined && <span className="chip">共 {data.total}</span>}</h1>{tab === 'users' && <div className="toolbar"><label>搜索用户名<input value={search} onChange={e => setSearch(e.target.value)} type="search"/></label><label>封禁状态<select value={banned} onChange={e => setBanned(e.target.value)}><option value="">全部</option><option value="true">已封禁</option><option value="false">正常</option></select></label><label>在线状态<select value={online} onChange={e => setOnline(e.target.value)}><option value="">全部</option><option value="true">在线</option><option value="false">离线</option></select></label></div>}{tab === 'rooms' && userId && <p>正在查看用户 {userId} 的参与对局 <button onClick={() => setUserId('')}>查看全部</button></p>}<button disabled={busy} onClick={() => setRev(x => x + 1)}>刷新</button><div className="table-wrap" aria-busy={busy}><table><thead><tr>{tab === 'users' ? <><th>用户名 / ID</th><th>状态</th><th>注册时间</th><th>操作</th></> : <><th>记录</th><th>详情</th><th>操作</th></>}</tr></thead><tbody>{items.map((item: RecordData, i: number) => <tr key={item.id || item.slug || i}>{tab === 'users' ? <><td>{item.username}<br /><small>{item.id}</small></td><td>{item.online ? '在线' : '离线'} · {item.banned ? '已封禁' : '正常'}</td><td>{item.created_at}</td><td><button disabled={busy} onClick={() => { setTab('rooms'); setUserId(item.id); }}>参与房间</button> <button disabled={busy || item.id === me.id} onClick={() => { const note = prompt(`${item.banned ? '解封' : '封禁'} ${item.username} 的处理说明`); if (note?.trim())
        act(`/users/${item.id}/ban`, { banned: !item.banned, note }); }}>{item.banned ? '解封' : '封禁'}</button></td></> : <><td>{item.title || item.subject || item.reason || item.slug || item.id}<br /><small>{item.created_at}</small></td><td><RecordDetail item={item}/></td><td>{tab === 'shares' ? <button disabled={busy || !!item.revoked_at} onClick={() => { if (confirm('撤销此分享，后续访问将被拒绝？'))
        act(`/shares/${item.slug || item.id}/revoke`, {}); }}>{item.revoked_at ? '已撤销' : '撤销分享'}</button> : tab === 'reports' || tab === 'feedback' ? <><button disabled={busy || item.status !== 'open'} onClick={() => { const note = prompt('处理说明'); if (note?.trim())
        act(`/${tab}/${item.id}/resolve`, { status: 'resolved', note }); }}>处理完成</button> <button disabled={busy || item.status !== 'open'} onClick={() => { const note = prompt('驳回说明'); if (note?.trim())
        act(`/${tab}/${item.id}/resolve`, { status: 'dismissed', note }); }}>驳回</button></> : <span>只读</span>}</td></>}</tr>)}</tbody></table></div>{!busy && !items.length && <p>没有符合条件的记录。</p>}<nav className="pager" aria-label="管理列表分页"><button disabled={busy || !page} onClick={() => setPage(p => p - 1)}>上一页</button><span>第 {page + 1} 页</span><button disabled={busy || !data.next_cursor} onClick={() => { setCursors(c => [...c.slice(0, page + 1), data.next_cursor]); setPage(p => p + 1); }}>下一页</button></nav></section></>}</main></>;
}
function RecordDetail({ item }: {
    item: RecordData;
}) { return <details><summary>{item.status || item.access || '查看详情'}</summary><dl>{Object.entries(item).filter(([key]) => !['password', 'password_hash', 'token', 'csrf_token'].includes(key)).map(([key, value]) => <React.Fragment key={key}><dt>{key}</dt><dd style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value ?? '—')}</dd></React.Fragment>)}</dl></details>; }
createRoot(document.getElementById('root')!).render(<Admin />);
