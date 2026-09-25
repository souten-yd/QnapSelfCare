const {JSDOM} = require('jsdom');
const fs = require('node:fs');
const assert = require('node:assert/strict');
async function until(check) {
  for(let i=0;i<100;i++){if(check())return;await new Promise(r=>setTimeout(r,10));}
  throw Error('Migration UI timed out');
}
(async()=>{
  const dom=new JSDOM(fs.readFileSync('web/index.html','utf8'),{url:'http://nas:17863',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window,d=w.document,calls=[];let poll;
  let state={busy:false,can_restore:false};
  w.setInterval=fn=>{poll=fn;return 0;};
  w.fetch=async(path,options)=>{
    calls.push({path,options});
    if(options.method==='POST')state={busy:true,can_restore:true,phase:'applying',message:'構成を適用しています'};
    return{ok:true,json:async()=>structuredClone(state)};
  };
  try{
    w.eval(fs.readFileSync('web/migration.js','utf8'));
    await until(()=>d.getElementById('migration-message').textContent.includes('上のボタン'));
    assert.equal(calls.filter(c=>c.options.method==='POST').length,0);
    d.getElementById('migrate-homehub').click();d.getElementById('migrate-homehub').click();
    await until(()=>d.getElementById('migration-message').textContent.includes('適用'));
    const posts=calls.filter(c=>c.options.method==='POST');assert.equal(posts.length,1);
    assert.deepEqual(JSON.parse(posts[0].options.body),{action:'migrate'});
    assert.equal(posts[0].options.headers['X-SelfCare-Request'],'1');
    state={busy:false,can_restore:true,phase:'failed',message:'中断しました',backup:'/private/saved'};
    await poll();await until(()=>!d.getElementById('restore-homehub').disabled);
    assert.equal(d.getElementById('migrate-homehub').disabled,true);
    d.getElementById('restore-homehub').click();
    await until(()=>calls.filter(c=>c.options.method==='POST').length===2);
    assert.deepEqual(JSON.parse(calls.filter(c=>c.options.method==='POST')[1].options.body),{action:'restore'});
    console.log('Migration UI: explicit start, duplicate guard, interrupted-state recovery: passed');
  }finally{w.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
