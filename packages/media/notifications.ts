import type {ApiClient} from '../api-client/index';
const STORAGE_KEY='onstage.push.subscription';
interface Binding {id:string;owner:string;endpoint:string}
export interface PushState {supported:boolean;configured:boolean;subscribed:boolean;permission:NotificationPermission|'unsupported';roomIds:string[]}
function supported(){return window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window}
function binding():Binding|null{try{return JSON.parse(localStorage.getItem(STORAGE_KEY)||'null')}catch{return null}}
function forget(){try{localStorage.removeItem(STORAGE_KEY)}catch{}}
async function registration(){
 const r=await navigator.serviceWorker.getRegistration('/');
 if(!r?.active)throw new Error('消息通知服务尚未就绪，请更新缓存服务后重试。');
 await new Promise<void>((resolve,reject)=>{
  const channel=new MessageChannel();
  const fail=()=>finish(new Error('消息通知服务版本尚未就绪，请先在设置中更新美术缓存服务，再开启通知。'));
  const timer=setTimeout(fail,2000);
  const finish=(error?:Error)=>{clearTimeout(timer);channel.port1.close();channel.port2.close();error?reject(error):resolve()};
  channel.port1.onmessage=event=>event.data?.ok===true&&event.data?.result?.version===1?finish():fail();
  channel.port1.onmessageerror=fail;
  try{r.active!.postMessage({type:'PUSH_PROTOCOL'},[channel.port2])}catch{fail()}
 });
 return r;
}
export async function clearBrowserPush(api?:ApiClient){
 if('serviceWorker' in navigator){const r=await navigator.serviceWorker.getRegistration('/');const old=await r?.pushManager?.getSubscription();if(old && !await old.unsubscribe())throw new Error('未能关闭此浏览器的旧通知，请重试。')}
 const saved=binding();forget();
 // Logout also invalidates server subscriptions; a rotated session cannot delete an older session binding.
 if(api&&saved)try{await api.request(`/api/notifications/subscriptions/${encodeURIComponent(saved.id)}`,'DELETE')}catch{/* Browser endpoint is already unsubscribed. */}
}
export async function pushState(api:ApiClient,owner:string):Promise<PushState>{
 const config=await api.request('/api/notifications/config');
 const rooms=await api.request('/api/notifications/rooms');
 let subscribed=false;
 if(supported()){
  const saved=binding();const r=await navigator.serviceWorker.getRegistration('/');const sub=await r?.pushManager?.getSubscription();
  if(sub && (!saved || saved.owner!==owner)){await clearBrowserPush();}
  else subscribed=!!(sub&&saved?.endpoint===sub.endpoint);
 }
 return {supported:supported(),configured:config.enabled===true&&!!config.public_key,subscribed,permission:'Notification' in window?Notification.permission:'unsupported',roomIds:rooms.room_ids||[]};
}
export async function enableBrowserPush(api:ApiClient,owner:string){
 if(!supported())throw new Error('此浏览器不支持后台消息通知；请使用 HTTPS 和支持 Web Push 的浏览器。');
 // The permission request is reached only directly from the enable button.
 const permission=await Notification.requestPermission();
 if(permission!=='granted')throw new Error(permission==='denied'?'通知权限已被拒绝，请在浏览器设置中修改。':'尚未允许通知。');
 const config=await api.request('/api/notifications/config');
 if(config.enabled!==true||!config.public_key)throw new Error('服务器尚未配置消息通知。');
 const r=await registration();
 await clearBrowserPush(api);
 const raw=String(config.public_key).replace(/-/g,'+').replace(/_/g,'/');
 const bytes=Uint8Array.from(atob(raw+'='.repeat((4-raw.length%4)%4)),c=>c.charCodeAt(0));
 const sub=await r.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:bytes});
 try{const saved=await api.request('/api/notifications/subscriptions','POST',{endpoint:sub.endpoint,keys:sub.toJSON().keys});localStorage.setItem(STORAGE_KEY,JSON.stringify({id:saved.id,owner,endpoint:sub.endpoint}));}
 catch(error){await sub.unsubscribe();forget();throw error}
}
export async function setRoomPush(api:ApiClient,roomId:string,enabled:boolean){
 if(!/^[a-f0-9]{32}$/.test(roomId))throw new Error('房间标识无效。');
 await api.request(`/api/rooms/${roomId}/notifications`,'POST',{enabled});
}
