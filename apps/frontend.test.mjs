import assert from 'node:assert/strict';
import {test} from 'node:test';
// In-memory MessageChannel double: browser RPC semantics without Node-owned port handles.
class MessageChannel {constructor(){const port=()=>({closed:false,onmessage:null,onmessageerror:null,close(){this.closed=true}});this.port1=port();this.port2=port();this.port2.postMessage=data=>queueMicrotask(()=>{if(!this.port1.closed)this.port1.onmessage?.({data})});}}
import {build} from 'esbuild';
import {JSDOM} from 'jsdom';
const web=(await build({entryPoints:['apps/web/src/main.tsx'],bundle:true,write:false,format:'iife',loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"production"'}})).outputFiles[0].text;
const admin=(await build({entryPoints:['apps/admin/src/main.tsx'],bundle:true,write:false,format:'iife',loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"production"'}})).outputFiles[0].text;
const wait=()=>new Promise(r=>setTimeout(r,50));
async function mount(code,url,handler,setup=()=>{}){const dom=new JSDOM('<div id="root"></div>',{url,runScripts:'outside-only',pretendToBeVisual:true});const w=dom.window;const calls=[];w.AbortSignal=AbortSignal;w.scrollTo=()=>{};w.fetch=async(path,options)=>{calls.push({path,options});const data=await handler(String(path),options);return {ok:!data?.status,status:data?.status||200,json:async()=>data?.body||data}};setup(w);w.eval(code);await wait();await wait();return {dom,w,calls,close:()=>dom.window.close()}}
const catalog={characters:[{id:'tomori',name:'灯'}],scenarios:[],stickers:[]};
test('visitor browse paginates on server and sends selected sort without requiring login',async()=>{const m=await mount(web,'https://web.neboer.site/',path=>path==='/api/session'?{csrf_token:'c'}:path==='/api/auth/me'?{status:401,body:{detail:'login required'}}:path==='/api/catalog'?catalog:path.includes('/api/rooms')?{rooms:[{id:'r',title:'舞台',status:'live'}],next_cursor:path.includes('cursor=')?null:'page2'}:{});try{assert.match(m.w.document.body.textContent,/舞台/);assert.doesNotMatch(m.w.document.body.textContent,/login required/);const select=m.w.document.querySelector('[aria-label="房间排序"]');select.value='rating';select.dispatchEvent(new m.w.Event('change',{bubbles:true}));await wait();assert(m.calls.some(c=>c.path.includes('sort=rating')));const next=[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='下一页');next.click();await wait();assert(m.calls.some(c=>c.path.includes('cursor=page2')&&c.path.includes('limit=24')));assert(!m.w.document.querySelector('iframe'))}finally{m.close()}});
test('registration submits username/password and opens skippable preferences',async()=>{const m=await mount(web,'https://web.neboer.site/',(path,o)=>path==='/api/session'?{csrf_token:'c'}:path==='/api/auth/me'?{status:401}:path==='/api/catalog'?catalog:path==='/api/auth/register'?{id:'u',username:'private'}:{rooms:[]});try{[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='注册').click();await wait();const form=m.w.document.querySelector('form');form.elements.username.value='private';form.elements.password.value='secure-password';form.dispatchEvent(new m.w.Event('submit',{bubbles:true,cancelable:true}));await wait();await wait();const call=m.calls.find(c=>c.path==='/api/auth/register');assert.deepEqual(JSON.parse(call.options.body),{username:'private',password:'secure-password'});assert.equal(call.options.credentials,'include');assert.equal(call.options.headers['X-CSRF-Token'],'c');assert.match(m.w.document.body.textContent,/跳过，不设偏好/)}finally{m.close()}});
test('share renderer escapes content and renders snapshot stickers without exposing account fields',async()=>{const m=await mount(web,'https://web.neboer.site/s/authorized',p=>p==='/api/auth/me'?{status:401}:p==='/api/catalog'?catalog:p.startsWith('/api/shares/')?{messages:[{author:'灯',text:'<script>bad()</script>',username:'secret-login'},{author:'祥子',content_type:'sticker',sticker:{image:'/static/sticker.webp',name:'你好'}}]}:{});try{assert.match(m.w.document.body.textContent,/<script>bad\(\)<\/script>/);assert.doesNotMatch(m.w.document.body.textContent,/secret-login/);assert.equal(m.w.document.querySelectorAll('script').length,0);assert.equal(m.w.document.querySelector('img[alt="你好"]')?.getAttribute('src'),'/static/sticker.webp')}finally{m.close()}});
test('admin lists users, searches, opens participation and submits audited ban request',async()=>{const m=await mount(admin,'https://admin.neboer.site/',p=>p==='/api/session'?{csrf_token:'c'}:p==='/api/auth/me'?{id:'admin',username:'operator',admin:true,csrf_token:'c'}:p.startsWith('/api/admin/users')?{total:1,users:[{id:'u',username:'someone',online:true,banned:false}]}:{items:[]});try{assert.match(m.w.document.body.textContent,/someone/);m.w.prompt=()=> '违反规则';[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='封禁').click();await wait();const call=m.calls.find(c=>c.path==='/api/admin/users/u/ban');assert.deepEqual(JSON.parse(call.options.body),{banned:true,note:'违反规则'});assert.equal(call.options.headers['X-CSRF-Token'],'c');[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='参与房间').click();await wait();assert(m.calls.some(c=>c.path.includes('/api/admin/rooms?')&&c.path.includes('user_id=u')))}finally{m.close()}});
test('public manifest loads before views; scenes and preference artwork use immutable optimized URLs',async()=>{
 const optimized='/static/assets/optimized/'+'a'.repeat(64)+'.webp';
 const m=await mount(web,'https://web.neboer.site/scenes',p=>p==='/static/assets-manifest.json'?{schema_version:1,assets:{'/static/assets/scene.png':{url:optimized},'/static/assets/avatar.png':{url:optimized}}}:p==='/api/session'?{csrf_token:'c'}:p==='/api/auth/me'?{id:'u',username:'private',onboarding_done:true}:p==='/api/catalog'?{...catalog,characters:[{id:'tomori',name:'灯',image:'/static/assets/avatar.png'}]}:p.startsWith('/api/scenarios')?{scenarios:[{id:'scene',name:'舞台',image:'/static/assets/scene.png'}]}:{});
 try{assert.equal(m.calls[0].path,'/static/assets-manifest.json');assert.equal(m.calls[0].options.credentials,'omit');assert.equal(m.calls[0].options.cache,'no-cache');const scene=m.w.document.querySelector('img.scene');assert.equal(scene.getAttribute('src'),optimized);scene.dispatchEvent(new m.w.Event('error'));assert.equal(scene.getAttribute('src'),'/static/assets/scene.png');[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='角色偏好').click();await wait();assert.equal(m.w.document.querySelector('.characters img').getAttribute('src'),optimized)}finally{m.close()}
});
test('shared sticker snapshots use optimized artwork and accessible label',async()=>{
 const optimized='/static/assets/optimized/'+'b'.repeat(64)+'.webp';
 const m=await mount(web,'https://web.neboer.site/s/art',p=>p==='/static/assets-manifest.json'?{schema_version:1,assets:{'/static/assets/sticker.png':{url:optimized}}}:p==='/api/auth/me'?{status:401}:p==='/api/catalog'?catalog:p.startsWith('/api/shares/')?{messages:[{author:'灯',content_type:'sticker',sticker:{image:'/static/assets/sticker.png',label:'谢谢'}}]}:{});
 try{assert.equal(m.w.document.querySelector('img[alt="谢谢"]').getAttribute('src'),optimized)}finally{m.close()}
});
test('PWA bootstrap registers without bulk download; cache work requires explicit action and shared controller deduplicates init',async()=>{
 const counts={init:0,download:0,cancel:0,clear:0,refresh:0};let listener;let state={supported:true,registered:true,busy:false,cached:2,cachedBytes:1000000,message:'缓存就绪'};
 const m=await mount(web,'https://web.neboer.site/',p=>p==='/api/auth/me'?{status:401}:p==='/api/catalog'?catalog:{},w=>{w.OnstagePWA={init:async o=>{counts.init++;listener=o.onStatus;listener(state);return state},getStatus:()=>state,refreshStatus:async()=>{counts.refresh++;listener?.(state);return state},downloadArtwork:async()=>{counts.download++;state={...state,busy:true,total:10,completed:1};listener(state);return state},cancelDownload:async()=>{counts.cancel++;state={...state,busy:false};listener(state);return state},clearArtwork:async()=>{counts.clear++;state={...state,cached:0,cachedBytes:0};listener(state);return state}}});
 try{assert.equal(counts.init,1);assert.equal(counts.download,0);[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='美术缓存设置').click();await wait();assert.match(m.w.document.body.textContent,/已缓存 2 张/);assert.equal(counts.download,0);[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='手动缓存全部美术').click();await wait();assert.equal(counts.download,1);assert.match(m.w.document.body.textContent,/已处理 1 \/ 10/);[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='取消缓存').click();await wait();assert.equal(counts.cancel,1);[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='清除美术缓存').click();await wait();assert.equal(counts.clear,1);assert.match(m.w.document.body.textContent,/已缓存 0 张/);const check=m.w.document.querySelector('.dialog input[type="checkbox"]');check.click();await wait();assert.equal(m.w.document.documentElement.dataset.reduceMotion,'true');await m.w.OnstageArtworkCache.init({onStatus:()=>{}});assert.equal(counts.init,1)}finally{m.close()}
});

test('Web Push requires explicit enable, subscribes with CSRF, and saves a per-room switch',async()=>{
 const room='a'.repeat(32);let permissionRequests=0,subscription=null,enabledRooms=[];
 const sub={endpoint:'https://push.example.test/opaque',toJSON:()=>({endpoint:'https://push.example.test/opaque',expirationTime:null,keys:{p256dh:'public',auth:'auth'}}),unsubscribe:async()=>{subscription=null;return true}};
 const m=await mount(web,'https://web.neboer.site/room/'+room,(p,o)=>p==='/api/auth/me'?{id:'u',username:'private',csrf_token:'c'}:p==='/api/catalog'?catalog:p==='/api/notifications/config'?{enabled:true,public_key:'AQID'}:p==='/api/notifications/rooms'?{room_ids:enabledRooms}:p==='/api/notifications/subscriptions'?{id:'sub1'}:p===`/api/rooms/${room}/notifications`?(enabledRooms=JSON.parse(o.body).enabled?[room]:[],{}):{},w=>{
  Object.defineProperty(w,'isSecureContext',{value:true});w.PushManager=function(){};w.Notification={permission:'default',requestPermission:async()=>{permissionRequests++;return 'granted'}};
  w.MessageChannel=MessageChannel;
  Object.defineProperty(w.navigator,'serviceWorker',{value:{getRegistration:async()=>({active:{postMessage:(message,ports)=>{assert.equal(message.type,'PUSH_PROTOCOL');ports[0].postMessage({ok:true,result:{version:1}})}},pushManager:{getSubscription:async()=>subscription,subscribe:async options=>{assert.equal(options.userVisibleOnly,true);subscription=sub;return sub}}})}});
 });
 try{
  assert.equal(permissionRequests,0);assert(!m.calls.some(c=>c.path==='/api/notifications/subscriptions'));
  [...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='美术缓存设置').click();await wait();
  assert.equal(permissionRequests,0);
  [...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='开启此浏览器消息通知').click();await wait();await wait();
  assert.equal(permissionRequests,1);assert.match(m.w.document.body.textContent,/此浏览器消息通知已开启/);
  const post=m.calls.find(c=>c.path==='/api/notifications/subscriptions');assert.equal(post.options.credentials,'include');assert.equal(post.options.headers['X-CSRF-Token'],'c');assert.deepEqual(JSON.parse(post.options.body),{endpoint:sub.endpoint,keys:{p256dh:'public',auth:'auth'}});
  const check=[...m.w.document.querySelectorAll('label')].find(l=>l.textContent.includes('接收当前房间')).querySelector('input');check.click();await wait();assert.deepEqual(enabledRooms,[room]);assert.equal(check.checked,true);
  [...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='关闭此浏览器消息通知').click();await wait();assert.equal(subscription,null);assert(m.calls.some(c=>c.path==='/api/notifications/subscriptions/sub1'&&c.options.method==='DELETE'));
 }finally{m.close()}
});

test('Web Push reports unconfigured servers without requesting permission or pretending enabled',async()=>{
 let prompts=0;
 const m=await mount(web,'https://web.neboer.site/',p=>p==='/api/auth/me'?{id:'u',username:'private'}:p==='/api/catalog'?catalog:p==='/api/notifications/config'?{enabled:false,public_key:null}:p==='/api/notifications/rooms'?{room_ids:[]}:{},w=>{
  Object.defineProperty(w,'isSecureContext',{value:true});w.PushManager=function(){};w.Notification={permission:'default',requestPermission:async()=>{prompts++;return 'granted'}};Object.defineProperty(w.navigator,'serviceWorker',{value:{getRegistration:async()=>undefined}});
 });
 try{[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='美术缓存设置').click();await wait();assert.match(m.w.document.body.textContent,/服务器尚未配置消息通知/);assert.equal([...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='开启此浏览器消息通知').disabled,true);assert.equal(prompts,0)}finally{m.close()}
});

test('logout revokes server push binding even if browser unsubscribe fails',async()=>{
 let logout=false;
 const m=await mount(web,'https://web.neboer.site/',p=>p==='/api/auth/me'?{id:'u',username:'private',csrf_token:'c'}:p==='/api/catalog'?catalog:p==='/api/auth/logout'?(logout=true,{}):{},w=>{
  Object.defineProperty(w.navigator,'serviceWorker',{value:{getRegistration:async()=>({pushManager:{getSubscription:async()=>({unsubscribe:async()=>false})}})}});
 });
 try{[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='退出').click();await wait();assert.equal(logout,true)}finally{m.close()}
});

test('an account switch removes another account browser subscription without prompting permission',async()=>{
 let unsubscribed=0,prompts=0;
 const m=await mount(web,'https://web.neboer.site/',p=>p==='/api/auth/me'?{id:'new-user',username:'private'}:p==='/api/catalog'?catalog:p==='/api/notifications/config'?{enabled:true,public_key:'AQID'}:p==='/api/notifications/rooms'?{room_ids:[]}:{},w=>{
  Object.defineProperty(w,'isSecureContext',{value:true});w.PushManager=function(){};w.Notification={permission:'granted',requestPermission:async()=>{prompts++;return 'granted'}};
  w.localStorage.setItem('onstage.push.subscription',JSON.stringify({id:'old',owner:'old-user',endpoint:'https://push.example.test/old'}));
  Object.defineProperty(w.navigator,'serviceWorker',{value:{getRegistration:async()=>({active:true,pushManager:{getSubscription:async()=>({endpoint:'https://push.example.test/old',unsubscribe:async()=>{unsubscribed++;return true}})}})}});
 });
 try{[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='美术缓存设置').click();await wait();assert.equal(unsubscribed,1);assert.equal(prompts,0);assert.equal(m.w.localStorage.getItem('onstage.push.subscription'),null);assert.match(m.w.document.body.textContent,/此浏览器消息通知未开启/)}finally{m.close()}
});

test('Web Push blocks old active workers and silent handshake timeouts before subscribing',async()=>{
 for(const reply of ['unknown-command','silent']){
  let subscribes=0;
  const m=await mount(web,'https://web.neboer.site/',p=>p==='/api/auth/me'?{id:'u',username:'private'}:p==='/api/catalog'?catalog:p==='/api/notifications/config'?{enabled:true,public_key:'AQID'}:p==='/api/notifications/rooms'?{room_ids:[]}:{},w=>{
   Object.defineProperty(w,'isSecureContext',{value:true});w.PushManager=function(){};w.Notification={permission:'granted',requestPermission:async()=> 'granted'};w.MessageChannel=MessageChannel;
   Object.defineProperty(w.navigator,'serviceWorker',{value:{getRegistration:async()=>({active:{postMessage:(_message,ports)=>{if(reply==='unknown-command')ports[0].postMessage({ok:false,error:'unknown_artwork_command'})}},waiting:{postMessage:()=>{throw Error('must not autoactivate waiting worker')}},pushManager:{getSubscription:async()=>null,subscribe:async()=>{subscribes++;throw Error('must not subscribe')}}})}});
  });
  try{[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='美术缓存设置').click();await wait();[...m.w.document.querySelectorAll('button')].find(b=>b.textContent==='开启此浏览器消息通知').click();await new Promise(resolve=>setTimeout(resolve,reply==='silent'?2100:100));assert.match(m.w.document.body.textContent,/请先在设置中更新美术缓存服务/);assert.equal(subscribes,0);assert(!m.calls.some(c=>c.path==='/api/notifications/subscriptions'));assert.doesNotMatch(m.w.document.body.textContent,/此浏览器消息通知已开启/)}finally{m.close()}
 }
});
