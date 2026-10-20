import React, { useEffect, useState, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiClient, RecordData, pageItems } from '../../../packages/api-client/index';
import './style.css';
import {Artwork,loadPublicMedia} from '../../../packages/media/index';
import {artworkCache,ArtworkStatus} from '../../../packages/media/pwa';
import {clearBrowserPush,enableBrowserPush,pushState,setRoomPush,PushState} from '../../../packages/media/notifications';
const api = new ApiClient();
const errorText = (e: unknown) => e instanceof Error ? e.message : '操作未完成，请重试';
const id = () => crypto.randomUUID();
function usePage(path: string, key: string) {
    const [data, setData] = useState<RecordData>({}), [error, setError] = useState(''), [busy, setBusy] = useState(false), [cursors, setCursors] = useState<string[]>(['']), [page, setPage] = useState(0), [revision, setRevision] = useState(0);
    useEffect(() => { setPage(0); setCursors(['']); }, [path]);
    useEffect(() => { let active = true; setBusy(true); setError(''); api.request(`${path}${path.includes('?') ? '&' : '?'}limit=24${cursors[page] ? `&cursor=${encodeURIComponent(cursors[page])}` : ''}`).then(d => { if (active)
        setData(d); }).catch(e => { if (active)
        setError(errorText(e)); }).finally(() => { if (active)
        setBusy(false); }); return () => { active = false; }; }, [path, page, revision]);
    return { items: pageItems(data, key), data, error, busy, page, refresh: () => setRevision(x => x + 1), pager: <nav className="pager" aria-label="分页"><button disabled={busy || !page} onClick={() => setPage(p => p - 1)}>上一页</button><span role="status">第 {page + 1} 页</span><button disabled={busy || !data.next_cursor} onClick={() => { setCursors(c => [...c.slice(0, page + 1), data.next_cursor]); setPage(p => p + 1); }}>下一页</button></nav> };
}
function Modal({ title, children, onClose }: {title:string;children:React.ReactNode;onClose:()=>void}) {
 const dialog=useRef<HTMLElement>(null);
 useEffect(()=>{
  const previous=document.activeElement as HTMLElement|null;
  dialog.current?.querySelector<HTMLElement>('input,button,select,textarea,a[href]')?.focus();
  const f=(e:KeyboardEvent)=>{
   if(e.key==='Escape'){e.preventDefault();onClose();}
   if(e.key==='Tab'){
    const controls=Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),a[href]')||[]);
    const first=controls[0],last=controls.at(-1);
    if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus()}
    else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus()}
   }
  };
  window.addEventListener('keydown',f);
  return()=>{window.removeEventListener('keydown',f);previous?.focus()};
 },[onClose]);
 return <div className="dialog-backdrop" onClick={onClose}><section ref={dialog} role="dialog" aria-modal="true" aria-label={title} className="dialog" onClick={e=>e.stopPropagation()}><button className="close" onClick={onClose} aria-label="关闭">×</button><h2>{title}</h2>{children}</section></div>
}
function App() {
    const [me, setMe] = useState<RecordData>({}), [ready, setReady] = useState(false), [error, setError] = useState(''), [route, setRoute] = useState(location.pathname), [modal, setModal] = useState(''), [catalog, setCatalog] = useState<RecordData>({ characters: [], stickers: [] }), [artwork,setArtwork]=useState<ArtworkStatus>({}), [reduceMotion,setReduceMotion]=useState(()=>{try{return localStorage.getItem('onstage.reduce-motion')==='true'}catch{return false}});
    useEffect(()=>{const cache=artworkCache();void cache?.init({onStatus:setArtwork});const refresh=()=>{if(!document.hidden)void cache?.refreshStatus()};window.addEventListener('focus',refresh);return()=>{cache?.unsubscribe?.(setArtwork);window.removeEventListener('focus',refresh)}},[]);
    useEffect(()=>{document.documentElement.dataset.reduceMotion=String(reduceMotion);try{localStorage.setItem('onstage.reduce-motion',String(reduceMotion))}catch{}},[reduceMotion]);
    const refreshMe = async () => { try {
        const m = await api.bootstrap();
        setMe({ ...m, user: m.user || (m.id ? m : null) });
        if ((m.user || m.id) && m.onboarding_done === false)
            setModal('preferences');
    }
    catch (e) {
        setError(errorText(e));
    }
    finally {
        setReady(true);
    } };
    useEffect(() => { refreshMe(); api.request('/api/catalog').then(setCatalog).catch(e => setError(errorText(e))); const pop = () => setRoute(location.pathname); window.addEventListener('popstate', pop); return () => window.removeEventListener('popstate', pop); }, []);
    const go = (path: string) => { history.pushState({}, '', path); setRoute(path); window.scrollTo(0, 0); };
    const user = me.user;
    const requireLogin = (action: () => void) => user ? action() : setModal('login');
    useEffect(() => { if (!user)
        return; const beat = () => api.request('/api/auth/heartbeat', 'POST', {}).catch(() => { }); beat(); const timer = setInterval(beat, 30000); return () => clearInterval(timer); }, [user?.id]);
    const changed = async () => { setModal(''); await refreshMe(); };
    return <><header className="header"><a href="/" className="brand" onClick={e => { e.preventDefault(); go('/'); }}><Artwork src="/static/onstage-mark.svg" alt=""/>上皮 ONSTAGE</a><nav><button onClick={() => go('/')}>房间</button><button onClick={() => go('/scenes')}>场景</button><button onClick={() => go('/about')}>关于我们</button></nav><button onClick={()=>{const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=theme;try{localStorage.setItem('onstage.theme',theme)}catch{}}}>切换深浅主题</button><button onClick={()=>{setModal('artwork');void artworkCache()?.refreshStatus()}}>美术缓存设置</button><div className="account">{user ? <><span>我的账户：{user.username} </span><button onClick={() => setModal('preferences')}>角色偏好</button> <button onClick={async () => { try {
        // Server logout revokes its push bindings even when a browser service is unavailable.
        try { await clearBrowserPush(api); } catch { /* Do not prevent logout. */ }
        await api.request('/api/auth/logout', 'POST', {});
        setMe({});
        await refreshMe();
        go('/');
    }
    catch (e) {
        setError(errorText(e));
    } }}>退出</button></> : <><button onClick={() => setModal('login')}>登录</button> <button className="primary" onClick={() => setModal('register')}>注册</button></>}</div></header><main className="layout"><div aria-live="polite">{error && <p className="error">{error} <button onClick={() => { setError(''); refreshMe(); }}>重试</button></p>}</div>{!ready ? <p role="status">正在连接…</p> : route.startsWith('/s/') ? <Share slug={route.slice(3)} catalog={catalog}/> : route.startsWith('/room/') ? <Room roomId={route.slice(6)} catalog={catalog} user={user} requireLogin={requireLogin}/> : route === '/scenes' ? <Scenes onCreate={s => requireLogin(() => setModal(`create:${s.id}`))}/> : route === '/about' ? <><section className="panel"><h1>关于上皮 ONSTAGE</h1><p>与真实的同伴，以喜欢的少女乐队角色自由对戏。</p><p>项目由 SpringfiledBucks 发起，源码和贡献记录见 <a href="https://github.com/SpringfiledBucks/onstage" target="_blank" rel="noreferrer">GitHub 项目</a>。角色、美术与贴纸的来源及权利说明见仓库 NOTICE 和内容来源文档。</p><p>本站并非原作官方服务。</p><button onClick={() => requireLogin(() => setModal('feedback'))}>向开发者反馈</button></section></> : <Lobby catalog={catalog} user={user} go={go} create={() => requireLogin(() => go('/scenes'))}/>}</main><footer className="layout">真人同场 · 自由接戏 · 美术缓存可减少重复下载，实时对戏仍需联网。</footer>{modal && <Modal title={modal === 'login' ? '登录' : modal === 'register' ? '注册' : modal === 'preferences' ? '喜欢扮演哪些角色？' : modal === 'feedback' ? '向开发者反馈' : modal==='artwork'?'美术缓存与显示设置':'创建房间'} onClose={() => setModal('')}>{modal === 'login' || modal === 'register' ? <Auth register={modal === 'register'} done={async () => { await changed(); if (modal === 'register')
        setModal('preferences'); }}/> : modal === 'preferences' ? <Preferences catalog={catalog} current={Array.isArray(me.preferences) ? me.preferences : me.preferences?.character_ids || me.favorite_character_ids || []} done={changed}/> : modal === 'feedback' ? <Feedback done={() => setModal('')}/> : modal==='artwork'?<><ArtworkSettings status={artwork} reduceMotion={reduceMotion} setReduceMotion={setReduceMotion}/><NotificationSettings user={user} roomId={route.startsWith('/room/')?route.slice(6):undefined}/></>: <Create scenarioId={modal.slice(7)} done={room => { setModal(''); go(`/room/${room.id}`); }}/>}</Modal>}</>;
}
function ArtworkSettings({status,reduceMotion,setReduceMotion}:{status:ArtworkStatus;reduceMotion:boolean;setReduceMotion:(v:boolean)=>void}){
 const cache=artworkCache();
 const [actionError,setActionError]=useState('');
 const perform=async(action:()=>Promise<unknown>|undefined)=>{setActionError('');try{await action()}catch(e){setActionError(errorText(e))}};
 return <><p>只保存公开美术，不保存账户、聊天或举报。不会自动下载全部素材；多人对戏仍需联网。</p><p role="status" aria-live="polite">{status.message||'美术缓存初始化中。'} {status.busy?`已处理 ${status.completed||0} / ${status.total||0} 张${status.failed?`，失败 ${status.failed} 张`:''}`:`已缓存 ${status.cached||0} 张，${((status.cachedBytes||0)/1000000).toFixed(2)} MB。`}</p>{(status.error||actionError)&&<p className="error" role="alert">{status.error||actionError}</p>}<div className="toolbar">{status.updateAvailable&&<button disabled={status.busy} onClick={()=>perform(()=>cache?.activateUpdate?.())}>更新美术缓存服务（保留草稿）</button>}<button disabled={!status.supported||!status.registered||status.busy} onClick={()=>perform(()=>cache?.downloadArtwork())}>手动缓存全部美术</button><button disabled={!status.busy} onClick={()=>perform(()=>cache?.cancelDownload())}>取消缓存</button><button disabled={!status.supported||!status.registered||status.busy} onClick={()=>perform(()=>cache?.clearArtwork())}>清除美术缓存</button><button onClick={()=>perform(()=>cache?.refreshStatus())}>刷新状态</button></div><label className="check"><input type="checkbox" checked={reduceMotion} onChange={e=>setReduceMotion(e.target.checked)}/>减少动态效果（同时尊重系统设置）</label></>
}
function NotificationSettings({user,roomId}:{user:any;roomId?:string}){
 const [state,setState]=useState<PushState>(),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const refresh=()=>pushState(api,String(user.id)).then(setState);
 useEffect(()=>{let active=true;if(user)pushState(api,String(user.id)).then(s=>{if(active)setState(s)}).catch(e=>{if(active)setError(errorText(e))});return()=>{active=false}},[user?.id]);
 const perform=async(action:()=>Promise<unknown>)=>{setBusy(true);setError('');try{await action();await refresh()}catch(e){setError(errorText(e))}finally{setBusy(false)}};
 return <section aria-label="消息通知"><h3>消息通知</h3><p>只提醒有新消息，不含正文、角色或账户信息。需要 HTTPS、浏览器推送服务和系统通知权限；部分手机需先将本站添加到主屏幕。页面关闭后能否接收还取决于浏览器与网络。</p>{!user?<p>登录后可为此浏览器和房间开启通知。</p>:<>{error&&<p role="alert" className="error">{error}</p>}{state&&<><p role="status">{!state.supported?'此浏览器不支持后台消息通知。':!state.configured?'服务器尚未配置消息通知。':state.subscribed?'此浏览器消息通知已开启。':state.permission==='denied'?'浏览器已拒绝通知，请前往浏览器设置修改。':'此浏览器消息通知未开启。'}</p><button disabled={busy||!state.supported||(!state.configured&&!state.subscribed)} onClick={()=>perform(()=>state.subscribed?clearBrowserPush(api):enableBrowserPush(api,String(user.id)))}>{state.subscribed?'关闭此浏览器消息通知':'开启此浏览器消息通知'}</button>{roomId&&/^[a-f0-9]{32}$/.test(roomId)&&<label className="check"><input type="checkbox" disabled={busy||!state.configured||!state.subscribed} checked={state.roomIds.includes(roomId)} onChange={e=>perform(()=>setRoomPush(api,roomId,e.target.checked))}/>接收当前房间的新消息通知</label>}</>}</>}</section>
}
function Auth({ register, done }: {
    register: boolean;
    done: () => void;
}) { const [error, setError] = useState(''), [busy, setBusy] = useState(false); return <form onSubmit={async (e) => { e.preventDefault(); const f = new FormData(e.currentTarget); setBusy(true); setError(''); try {
    await clearBrowserPush(api);
    await api.request(`/api/auth/${register ? 'register' : 'login'}`, 'POST', { username: f.get('username'), password: f.get('password'), ...(!register ? { remember: f.has('remember') } : {}) });
    done();
}
catch (e) {
    setError(errorText(e));
}
finally {
    setBusy(false);
} }}><label>用户名<input name="username" required autoComplete="username" minLength={3} maxLength={32}/></label><label>密码<input name="password" type="password" required minLength={8} maxLength={128} autoComplete={register ? 'new-password' : 'current-password'}/></label>{!register && <label className="check"><input name="remember" type="checkbox"/>记住我（仅在私人设备勾选）</label>}{register && <p className="muted">登录用户名不会作为房间里的公开身份。无需邮箱或手机验证，请妥善保存密码。</p>}{error && <p className="error" role="alert">{error}</p>}<button className="primary" disabled={busy}>{busy ? '正在处理…' : register ? '注册' : '登录'}</button></form>; }
function Preferences({ catalog, current, done }: {
    catalog: RecordData;
    current: string[];
    done: () => void;
}) { const [selected, setSelected] = useState<string[]>(current), [error, setError] = useState(''), [busy, setBusy] = useState(false); const save = async (values: string[]) => { setBusy(true); try {
    await api.request('/api/auth/preferences', 'POST', { character_ids: values });
    done();
}
catch (e) {
    setError(errorText(e));
}
finally {
    setBusy(false);
} }; return <><p>优先发现正在招募这些角色的房间，之后也可以修改。</p><div className="characters">{catalog.characters?.map((c: RecordData) => <label key={c.id}><input type="checkbox" checked={selected.includes(c.id)} onChange={e => setSelected(s => e.target.checked ? [...s, c.id] : s.filter(x => x !== c.id))}/><Artwork src={c.avatar_image || c.image} alt="" loading="lazy"/>{c.name}</label>)}</div>{error && <p role="alert">{error}</p>}<div className="toolbar"><button className="primary" disabled={busy} onClick={() => save(selected)}>保存偏好</button><button disabled={busy} onClick={() => save([])}>跳过，不设偏好</button></div></>; }
function Lobby({ user, go, create, catalog }: { catalog: RecordData;
    user: any;
    go: (p: string) => void;
    create: () => void;
}) { const [filter, setFilter] = useState('active'), [sort, setSort] = useState('time'), [preferred, setPreferred] = useState(false); const p = usePage(`/api/rooms?filter=${filter}&sort=${sort}&preferred=${preferred}`, 'rooms'); return <><section className="hero"><div className="hero-copy"><p className="muted">MYGO!!!!! × AVE MUJICA</p><h1>下一句，<br />轮到你。</h1><p>选择喜欢的角色，接住彼此的回应。</p><button className="primary" onClick={create}>选个场景开聊</button></div><div className="hero-bands">{catalog.bands?.map((band:RecordData)=><div key={band.id}><Artwork src={band.image} alt={band.name} loading="lazy"/><span>{band.name}</span></div>)}</div></section><section aria-label="房间目录"><div className="toolbar"><h2>正在发生</h2><select aria-label="房间筛选" value={filter} onChange={e => setFilter(e.target.value)}><option value="active">全部现场</option><option value="ended">已结束</option>{user && <option value="mine">我参与的</option>}</select><select aria-label="房间排序" value={sort} onChange={e => setSort(e.target.value)}><option value="time">最近创建</option><option value="rating">评分最高</option><option value="dialogue">对话最多</option></select>{user && <label className="check"><input type="checkbox" checked={preferred} onChange={e => setPreferred(e.target.checked)}/>偏好角色优先</label>}<button onClick={p.refresh} disabled={p.busy}>刷新</button></div>{p.error && <p role="alert" className="error">{p.error}</p>}<div className="grid" aria-busy={p.busy}>{p.items.map((r: RecordData) => <article className="card" key={r.id}><span className="chip">{r.status === 'ended' ? '已落幕' : r.status === 'live' ? '开幕中' : '候场中'}</span><h3>{r.title || '未命名现场'}</h3><p className="muted">{r.member_count ?? 0} 位在房成员 · {r.dialogue_count ?? r.message_count ?? 0} 条舞台对话</p><p>评分 {r.rating_average ?? r.rating_avg ?? r.rating ?? '—'}（{r.rating_count ?? 0} 人）{r.access && r.access !== 'public' ? ` · ${r.access === 'password' ? '密码房' : '邀请房'}` : ''}</p><button onClick={() => go(`/room/${r.id}`)}>查看房间</button></article>)}</div>{!p.busy && !p.items.length && <p>还没有符合条件的房间。</p>}{p.pager}</section></>; }
function Scenes({ onCreate, actionLabel="在这里开场" }: {
    actionLabel?: string;
    onCreate: (s: RecordData) => void;
}) { const p = usePage('/api/scenarios', 'scenarios'); return <><h1>故事，从哪里开始？</h1><p>场景是一块背景板，故事由你们决定。</p>{p.error && <p className="error">{p.error}</p>}<div className="grid" aria-busy={p.busy}>{p.items.map((s: RecordData) => <article className="card" key={s.id}>{s.image && <Artwork className="scene" src={s.image} alt="" loading="lazy"/>}<h2>{s.name || s.title}</h2><p>{s.description || s.opening}</p><button onClick={() => onCreate(s)}>{actionLabel}</button></article>)}</div>{p.pager}</>; }
function Create({ scenarioId, done }: {
    scenarioId: string;
    done: (r: RecordData) => void;
}) { const [error, setError] = useState(''), [busy, setBusy] = useState(false), [access,setAccess]=useState('public'); return <form onSubmit={async (e) => { e.preventDefault(); const f = new FormData(e.currentTarget); setBusy(true); try {
    const d = await api.request('/api/rooms', 'POST', { scenario_id: scenarioId, title: f.get('title'), background: f.get('background'), announcement: '', access, password: access==='password'?f.get('password'):undefined });
    done(d.room);
}
catch (e) {
    setError(errorText(e));
}
finally {
    setBusy(false);
} }}><label>房间标题<input name="title" maxLength={80} required/></label><label>开场背景<textarea name="background" maxLength={2000}/></label><label>入场方式<select value={access} onChange={e=>setAccess(e.target.value)}><option value="public">公开房间</option><option value="password">凭密码加入</option><option value="invite">仅凭邀请加入</option></select></label>{access==='password'&&<label>房间密码<input name="password" type="password" autoComplete="new-password" minLength={8} maxLength={128} required/></label>}<p className="muted">私密房从创建起即受保护；进入后可生成邀请和设置角色招募。</p>{error && <p role="alert" className="error">{error}</p>}<button disabled={busy} className="primary">创建房间</button></form>; }
function Feedback({ done }: {
    done: () => void;
}) { const [error, setError] = useState(''), [busy, setBusy] = useState(false); return <form onSubmit={async (e) => { e.preventDefault(); const f = new FormData(e.currentTarget); setBusy(true); try {
    await api.request('/api/feedback', 'POST', { text: `${f.get('subject')}\n\n${f.get('details')}` });
    done();
}
catch (e) {
    setError(errorText(e));
}
finally {
    setBusy(false);
} }}><label>主题<input name="subject" maxLength={120} required/></label><label>问题或建议<textarea name="details" maxLength={1800} required/></label><p className="muted">请勿填写密码、令牌或其他人的隐私。</p>{error && <p className="error">{error}</p>}<button disabled={busy} className="primary">提交反馈</button></form>; }
function Message({ message: m, catalog }: {
    message: RecordData;
    catalog: RecordData;
}) { const sticker = m.sticker || catalog.stickers?.find((s: RecordData) => s.id === m.sticker_id); return <div className="body"><strong>{(typeof m.author === "string" ? m.author : m.author?.display_name || m.author?.name) || m.display_name || m.nickname || m.role_name || m.author_name || '舞台成员'}</strong><div>{m.content_type === 'sticker' ? (sticker ? <Artwork src={sticker.image || sticker.url} alt={sticker.label || sticker.name || '贴纸'} loading="lazy"/> : <span>贴纸 {m.sticker_id}</span>) : m.text}</div></div>; }
function Share({ slug, catalog }: {
    slug: string;
    catalog: RecordData;
}) { const [data, setData] = useState<RecordData>({}), [error, setError] = useState(''); useEffect(() => { api.request(`/api/shares/${encodeURIComponent(slug)}`).then(setData).catch(e => setError(errorText(e))); }, [slug]); const s = data.share || data; return <section className="panel"><h1>舞台记录片段</h1><p className="muted">仅包含作者授权公开的消息。</p>{error && <p className="error" role="alert">{error}</p>}<ol className="messages">{(s.messages || data.messages || []).map((m: RecordData, i: number) => <li className="message" key={m.id || i}><Message message={m} catalog={catalog}/></li>)}</ol></section>; }
function Room({ roomId, catalog, user, requireLogin }: {
    roomId: string;
    catalog: RecordData;
    user: any;
    requireLogin: (a: () => void) => void;
}) {
    const [room, setRoom] = useState<RecordData | null>(null), [error, setError] = useState(''), [notice, setNotice] = useState(''), [tab, setTab] = useState('chat'), [messages, setMessages] = useState<RecordData[]>([]), [cursor, setCursor] = useState(0), [more, setMore] = useState(false), [selected, setSelected] = useState<string[]>([]), [shareLink, setShareLink] = useState(''), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0), [loaded, setLoaded] = useState(false), [participants, setParticipants] = useState<RecordData[]>([]), [historicalMe, setHistoricalMe] = useState<RecordData | null>(null);
    const load = async () => { try {
        const d = await api.request(`/api/rooms/${roomId}`);
        setRoom(d.room);
        setError('');
        if (user)
            api.request(`/api/rooms/${roomId}/participants`).then(p => { setParticipants(p.members || []); setHistoricalMe(p.me || (p.own_member_id ? { member_id: p.own_member_id } : null)); }).catch(() => { });
    }
    catch (e) {
        setError(errorText(e));
    }
    finally {
        setLoaded(true);
    } };
    useEffect(() => { setRoom(null); setLoaded(false); load(); }, [roomId, refresh]);
    useEffect(() => { if (!user || !room?.me)
        return; const connection_id = id(); const beat = () => api.request(`/api/rooms/${roomId}/heartbeat`, 'POST', { connection_id }).catch(() => { }); beat(); const timer = setInterval(beat, 20000); return () => clearInterval(timer); }, [roomId, user?.id, room?.me?.member_id]);
    useEffect(()=>{
      const receive=(event:MessageEvent)=>{
        const frame=document.querySelector<HTMLIFrameElement>('iframe.play');
        if(event.origin===location.origin&&event.source===frame?.contentWindow&&event.data?.type==='onstage:room-updated'&&event.data.room_id===roomId)load();
      };
      window.addEventListener('message',receive);
      return()=>window.removeEventListener('message',receive);
    },[roomId,user?.id]);
    const act = async (path: string, body: unknown) => { setBusy(true); setError(''); setNotice(''); try {
        const d = await api.request(`/api/rooms/${roomId}${path}`, 'POST', body);
        if (d.room)
            setRoom(d.room);
        else
            await load();
        return d;
    }
    catch (e) {
        setError(errorText(e));
        return null;
    }
    finally {
        setBusy(false);
    } };
    const history = async (reset = false) => { setBusy(true); try {
        const d = await api.request(`/api/rooms/${roomId}/messages?channel=stage&after=${reset ? 0 : cursor}&limit=100`);
        setMessages(old => reset ? d.messages : [...old, ...d.messages.filter((m: RecordData) => !old.some(x => x.id === m.id))]);
        setCursor(d.cursor);
        setMore(d.has_more);
        setTab('history');
    }
    catch (e) {
        setError(errorText(e));
    }
    finally {
        setBusy(false);
    } };
    const myMembership = room?.me ? { ...historicalMe, ...room.me } : historicalMe;
    const host = room?.me?.is_host;
    const members = participants.length ? participants : room?.members || [];
    return <><h1>{room?.title || '房间'}</h1>{error && <p className="error" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}{!user ? <div className="panel"><p>登录后可以入场、对戏及查看有权访问的完整记录。</p><button className="primary" onClick={() => requireLogin(() => { })}>登录后入场</button></div> : <>{loaded && !room && <form className="panel" onSubmit={async (e) => { e.preventDefault(); const f = new FormData(e.currentTarget); const d = await act('/unlock', { password: f.get('password') || undefined, invite: f.get('invite') || undefined }); if (d)
        load(); }}><p>若这是私密房间，请输入密码或邀请码。</p><label>房间密码<input name="password" type="password" autoComplete="off"/></label><label>邀请码<input name="invite" autoComplete="off" defaultValue={new URLSearchParams(location.search).get('invite') || ''}/></label><button disabled={busy}>解锁</button></form>}{room && <><div className="toolbar"><span className="chip">{room.status === 'ended' ? '已结束' : room.status === 'live' ? '开幕中' : '候场中'}</span><button onClick={() => setTab('chat')}>正常对戏</button>{myMembership && <button onClick={() => history(true)}>回看 / 选择分享</button>}<button onClick={load}>刷新房间</button></div>{!room.me && room.status !== 'ended' && <button className="primary" disabled={busy} onClick={() => act('/join', {})}>进入房间</button>}{host && <details className="panel room-tools"><summary>房主设置：场景、招募、访问与公开列表</summary><HostControls room={room} catalog={catalog} act={act} busy={busy}/></details>}{myMembership && <details className="panel"><summary>评分、成员反馈与分享许可</summary>{room.status === 'ended' && <label>匿名评分<select defaultValue="" disabled={busy} onChange={async (e) => { if (e.target.value && await act('/rating', { rating: Number(e.target.value) }))
        setNotice('评分已保存。公开显示汇总，不显示评分账户。'); }}><option value="">请选择</option>{[1, 2, 3, 4, 5].map(n => <option key={n} value={n}>{n} 分</option>)}</select></label>}<label className="check"><input type="checkbox" checked={!!(myMembership.share_allowed ?? myMembership.share_permission)} disabled={busy} onChange={e => act('/share-permission', { allowed: e.target.checked })}/>允许他人分享我在本局的舞台消息（可撤销）</label><p className="muted">赞踩和举报仅管理员可见。不要在原因中填写密码等隐私。</p>{members.filter((m: RecordData) => m.id !== myMembership.member_id).map((m: RecordData) => <div className="toolbar" key={m.id}><span>{m.display_name || m.alias || '成员'}</span><button disabled={busy} onClick={async () => { if (await act(`/members/${m.id}/vote`, { value: 1 }))
        setNotice('点赞已保存，仅管理员可见。'); }}>点赞</button><button disabled={busy} onClick={async () => { if (await act(`/members/${m.id}/vote`, { value: -1 }))
        setNotice('点踩已保存，仅管理员可见。'); }}>点踩</button><button disabled={busy} onClick={() => act(`/members/${m.id}/vote`, { value: 0 })}>撤回评价</button><button disabled={busy} onClick={async () => { const details = prompt('举报原因（仅管理员可见）'); if (details && await act(`/members/${m.id}/report`, { reason: 'other', details }))
        setNotice('举报已提交。'); }}>举报</button></div>)}</details>}{tab === 'chat' ? <iframe key={roomId} title="Onstage 对戏舞台" className="play" src={`/play/index.html#room=${encodeURIComponent(roomId)}`}/> : <section className="panel"><div className="toolbar"><button onClick={() => { document.getElementById('history-start')?.scrollIntoView(); }}>跳到开头</button><button onClick={() => history(true)} disabled={busy}>重新读取</button><button disabled={busy || !selected.length} onClick={async () => { const d = await act('/shares', { message_ids: selected }); if (d) {
        const s = d.share || d;
        setShareLink(`${location.origin}/s/${s.slug || d.slug}`);
        setSelected([]);
    } }}>分享所选 {selected.length} 条</button></div><p className="muted">只能分享已获作者许可的舞台消息，最多选择 100 条。</p>{shareLink && <p className="notice"><a href={shareLink}>{shareLink}</a> <button disabled={busy} onClick={async()=>{try{await api.request(`/api/shares/${shareLink.split('/').pop()}/revoke`,'POST',{});setShareLink('');setNotice('分享已撤销。')}catch(e){setError(errorText(e))}}}>撤销此分享</button></p>}<ol className="messages" id="history-start">{messages.map(m => <li className="message" key={m.id}><label><input type="checkbox" aria-label={`选择消息 ${m.seq ?? m.id}`} checked={selected.includes(m.id)} disabled={m.kind!=='speech'||(!selected.includes(m.id) && selected.length >= 100)} onChange={e => setSelected(s => e.target.checked ? [...s, m.id] : s.filter(x => x !== m.id))}/><Message message={m} catalog={catalog}/></label></li>)}</ol>{more && <button disabled={busy} onClick={() => history()}>继续读取后面的消息</button>}</section>}</>}</>}</>;
}
function HostControls({ room, catalog, act, busy }: {
    room: RecordData;
    catalog: RecordData;
    act: (p: string, b: unknown) => Promise<any>;
    busy: boolean;
}) { const [selected, setSelected] = useState<string[]>(room.recruitment?.character_ids || room.recruitment || []), [invite, setInvite] = useState(''), [scene, setScene] = useState(false); return <><div className="toolbar"><button disabled={busy} onClick={() => setScene(s => !s)}>切换场景</button>{room.status === 'ended' && <button disabled={busy} onClick={() => act('/visibility', { hidden: !room.hidden })}>{room.hidden ? '恢复公开列出' : '从公开列表隐藏'}</button>}</div>{scene && <Scenes onCreate={async (s) => { if (await act('/scene', { scenario_id: s.id, action_id: id() }))
    setScene(false); }}/>}<form onSubmit={e => { e.preventDefault(); const f = new FormData(e.currentTarget); act('/access', { access: f.get('access'), password: f.get('password') || undefined, share_enabled: f.has('share_enabled') }); }}><label>入场方式<select name="access" defaultValue={room.access || 'public'}><option value="public">公开</option><option value="password">密码</option><option value="invite">仅邀请</option></select></label><label>新房间密码<input name="password" type="password" autoComplete="new-password" minLength={8}/></label><label className="check"><input type="checkbox" name="share_enabled" defaultChecked={!!room.share_enabled}/>允许经作者授权的舞台分享</label><button disabled={busy}>保存访问设置</button></form><div className="toolbar"><button disabled={busy} onClick={async () => { const d = await act('/invites', { expires_hours: 24, max_uses: 10 }); if (d) {
    const token = d.token || (typeof d.invite === 'string' ? d.invite : d.invite?.token);
    setInvite(token ? `${location.origin}/room/${room.id}?invite=${encodeURIComponent(token)}` : '邀请码已创建，请查看返回的有效邀请信息。');
} }}>创建 24 小时邀请（最多 10 次）</button></div>{invite && <p className="notice">{invite}</p>}<h3>招募角色</h3><div className="characters">{catalog.characters?.map((c: RecordData) => <label key={c.id}><input type="checkbox" checked={selected.includes(c.id)} onChange={e => setSelected(s => e.target.checked ? [...s, c.id] : s.filter(x => x !== c.id))}/>{c.name}</label>)}</div><button disabled={busy} onClick={() => act('/recruitment', { character_ids: selected })}>保存招募</button></>; }
void loadPublicMedia().then(()=>createRoot(document.getElementById('root')!).render(<App />));
