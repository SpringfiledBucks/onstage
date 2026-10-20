'use strict';
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const handlers={};const notifications=[];const navigations=[];let windows=[];let closed=0;
const origin='https://web.neboer.site';
const self={location:{origin},addEventListener:(type,fn)=>handlers[type]=fn,registration:{showNotification:async(title,options)=>notifications.push({title,options})},clients:{matchAll:async()=>windows,openWindow:async url=>navigations.push(['open',url])}};
vm.runInNewContext(fs.readFileSync('static/sw.js','utf8'),{self,URL,Set,Promise,Request,Response,Headers,AbortController,setTimeout,clearTimeout,crypto:require('node:crypto').webcrypto});
async function push(value,malformed=false){let work;handlers.push({data:{json:()=>{if(malformed)throw Error('invalid');return value}},waitUntil:p=>work=p});await work;return notifications.at(-1)}
async function click(data){let work;handlers.notificationclick({notification:{data,close:()=>closed++},waitUntil:p=>work=p});await work}
(async()=>{
 let handshake,handshakeWork;
 handlers.message({source:{url:origin+'/'},data:{type:'PUSH_PROTOCOL'},ports:[{postMessage:value=>handshake=value}],waitUntil:p=>handshakeWork=p});
 await handshakeWork;assert.equal(handshake.ok,true);assert.equal(handshake.result.version,1);
 let externalReply=false;handlers.message({source:{url:'https://evil.test/'},data:{type:'PUSH_PROTOCOL'},ports:[{postMessage:()=>externalReply=true}],waitUntil:()=>{throw Error('must reject cross-origin RPC')}});assert.equal(externalReply,false);
 const room='a'.repeat(32);
 let note=await push({type:'room_message',room_id:room,text:'private body',username:'private-name',url:'https://evil.test'});
 assert.equal(note.title,'Onstage');assert.equal(note.options.body,'你关注的房间有新消息');assert.equal(note.options.data.room_id,room);assert(!JSON.stringify(note).includes('private'));
 await push(null,true);assert.equal(notifications.length,2);assert.equal(notifications.at(-1).options.data.room_id,null);
 await push({type:'room_message',room_id:'../../evil'});assert.equal(notifications.at(-1).options.data.room_id,null);
 await click({room_id:'https://evil.test',url:'https://evil.test'});assert.deepEqual(navigations.pop(),['open',origin+'/']);
 let focused=0;windows=[{url:'https://evil.test',navigate:()=>{throw Error('must not focus external')}},{url:origin+'/old',navigate:async url=>{navigations.push(['navigate',url]);return{focus:async()=>focused++}}}];
 await click({room_id:room,url:'https://evil.test'});assert.deepEqual(navigations.pop(),['navigate',origin+'/room/'+room]);assert.equal(focused,1);assert.equal(closed,2);
 windows=[{url:origin+'/old',navigate:async()=>null}];await click({room_id:room});assert.deepEqual(navigations.pop(),['open',origin+'/room/'+room]);
 console.log('Web Push worker: identifier-only visible notifications and safe same-origin click routing passed');
})().catch(e=>{console.error(e);process.exitCode=1});
