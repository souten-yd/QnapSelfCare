// DOM integration test against an isolated real HTTP server. No NAS data is used.
const {JSDOM} = require('jsdom');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const data = fs.mkdtempSync(path.join(os.tmpdir(),'selfcare-ui-'));
const child = spawn(process.env.PYTHON || 'python3',['webapp.py','--port','18863','--data-dir',data],{stdio:['ignore','pipe','pipe']});
const base='http://127.0.0.1:18863';
const jsonResponse=(v,status=200)=>Promise.resolve(new Response(JSON.stringify(v),{status,headers:{'Content-Type':'application/json'}}));
// Fake SelfCare AI job API: POST /api/ai/jobs then poll /api/ai/jobs/status until completed.
function aiJobFetch(before,answerFor,stages=['completed']){
 const f=(url,init)=>{
  if(url==='/api/ai/jobs'&&init?.method==='POST'){const body=JSON.parse(init.body);f.bodies.push(body);const id='job_'+String(f.bodies.length).padStart(32,'0');f.answers[id]=answerFor(body);f.calls[id]=0;return jsonResponse({id,state:'queued',stage:'queued',elapsed_seconds:0},202);}
  if(typeof url==='string'&&url.startsWith('/api/ai/jobs/status?id=')){const id=decodeURIComponent(url.split('=')[1]);const stage=stages[Math.min(f.calls[id]++,stages.length-1)];
   return jsonResponse(stage==='completed'?{id,state:'completed',stage:'generating',elapsed_seconds:420,result:{answer:f.answers[id]}}:{id,state:'running',stage,elapsed_seconds:400,detail:'空きRAM 8.0 GiB'});}
  return before(url,init);};
 f.bodies=[];f.answers={};f.calls={};return f;}
async function until(check,tries=100){for(let i=0;i<tries;i++){if(await check())return;await new Promise(r=>setTimeout(r,50));}throw Error('UI operation timed out');}
(async()=>{let dom;try{
 await until(async()=>{try{return (await fetch(base+'/api/status')).ok;}catch{return false;}});
 const cursorAsset=await fetch(base+'/chart-cursor.js');
 assert.equal(cursorAsset.status,200);
 assert.match(await cursorAsset.text(),/attachChartCursor/);
 dom=new JSDOM(fs.readFileSync('web/index.html','utf8'),{url:base,runScripts:'outside-only'});
 const w=dom.window,d=w.document;
 const appSource=fs.readFileSync('web/app.js','utf8');
 assert.equal(appSource.includes("$('jobs').replaceChildren()"),false);
 w.TextDecoder=TextDecoder;
 w.fetch=(url,init)=>fetch(base+url,init);w.setInterval=()=>0;w.confirm=()=>true;w.HTMLElement.prototype.scrollIntoView=()=>{};
 w.eval(fs.readFileSync('web/chart-cursor.js','utf8')+'\n'+fs.readFileSync('web/app.js','utf8')+'\n'+fs.readFileSync('web/wellness.js','utf8')+'\nwindow.__testRefreshJobs=refreshJobs;window.__testMark=markMeasuredToday;window.__testRestorePeriod=restorePeriodFilter;window.__testRefreshProposals=refreshProposals;window.__testRefreshAiActivity=refreshAiActivity;window.__testAiProgressText=aiProgressText;');
 await until(()=>d.getElementById('message').textContent.includes('利用者を追加'));
 const submit=id=>d.getElementById(id).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 d.getElementById('user-form').elements.name.value='DOM Test';submit('user-form');
 await until(()=>d.getElementById('message').textContent==='利用者を追加しました');
 d.querySelector('[data-page="records"]').click();
 const form=d.getElementById('record-form');
 form.elements.measured_at.value='2026-09-25T07:30:12';form.elements.systolic.value='130';form.elements.diastolic.value='80';form.elements.pulse.value='72';submit('record-form');
 await until(()=>d.getElementById('message').textContent.includes('1件保存'));
 await until(()=>d.getElementById('history').textContent.includes('最高血圧 130'));
 assert.match(d.getElementById('latest-bp').textContent,/130 \/ 80/);
 // The dashboard inspector must display exact measured values for both BP series.
 const bpSvg=d.querySelector('#bp-chart svg.chart-cursor-svg');
 assert(bpSvg);
 bpSvg.getBoundingClientRect=()=>({left:0,width:500});
 const tap=new w.Event('pointerdown',{bubbles:true});
 Object.defineProperties(tap,{clientX:{value:230},pointerId:{value:1},button:{value:0}});
 bpSvg.dispatchEvent(tap);
 assert.match(d.querySelector('#bp-chart .chart-cursor-readout').textContent,/最高血圧 130 mmHg/);
 assert.match(d.querySelector('#bp-chart .chart-cursor-readout').textContent,/最低血圧 80 mmHg/);
 assert.equal(bpSvg.querySelector('.chart-cursor-overlay').getAttribute('visibility'),'visible');
 // The Tokyo calendar day controls each badge, including the UTC day boundary.
 w.__testMark('bp-today-star',{measured_at:'2026-09-26T15:00:00Z'},new Date('2026-09-27T00:01:00Z'));
 assert.equal(d.getElementById('bp-today-star').hidden,false);
 assert.equal(d.getElementById('weight-today-star').hidden,true);
 w.__testMark('weight-today-star',{measured_at:'2026-09-26T14:59:00Z'},new Date('2026-09-27T00:01:00Z'));
 assert.equal(d.getElementById('weight-today-star').hidden,true);
 w.__testMark('bp-today-star',null,new Date('2026-09-27T00:01:00Z'));
 assert.equal(d.getElementById('bp-today-star').hidden,true);
 d.getElementById('history').querySelector('.record-controls').open=true;
 d.getElementById('history').querySelector('button').click();form.elements.pulse.value='74';submit('record-form');
 await until(()=>d.getElementById('message').textContent==='記録を更新しました');
 await until(()=>d.getElementById('history').textContent.includes('脈拍 74'));
 d.querySelector('[data-page="devices"]').click();
 const device=d.getElementById('device-form');device.elements.name.value='Test cuff';device.elements.address.value='AA:BB:CC:DD:EE:FF';
 d.getElementById('bindings').querySelector('select').selectedIndex=1;submit('device-form');
 await until(()=>d.getElementById('message').textContent==='機器設定を保存しました');
 assert.match(d.getElementById('device-list').textContent,/Test cuff/);
 assert.match(d.getElementById('device-list').textContent,/診断付きペアリング/);
 assert.equal((await (await fetch(base+'/api/devices')).json())[0].transport,'homehub');
 const actualFetch=w.fetch;
 w.fetch=(url,init)=>url==='/api/jobs'&&init?.method==='POST'&&JSON.parse(init.body).action==='scan'?Promise.resolve(new Response(JSON.stringify({id:'scan-test',state:'queued'}),{headers:{'Content-Type':'application/json'}})):url==='/api/jobs'&&(!init||!init.method)?Promise.resolve(new Response(JSON.stringify([{
  id:'scan-test',action:'scan',state:'done',created_at:new Date().toISOString(),message:'2台検出しました',
  result:JSON.stringify({adapter:'hci1',transport:'homehub',devices:[
   {name:'BLEsmart_0001000B123456',address:'11:22:33:44:55:66',rssi:-40},
   {name:'名前なし',address:'11:22:33:44:55:77',rssi:-65}]})
 }]),{headers:{'Content-Type':'application/json'}})):actualFetch(url,init);
 d.getElementById('scan').click();
 await until(()=>d.querySelectorAll('#scan-results .scan-result').length===2);
 const candidates=d.querySelectorAll('#scan-results .scan-result');
 assert.equal(candidates.length,2);
 assert.equal(candidates[0].querySelector('select').value,'HBF-228T');
 assert.equal(candidates[1].querySelector('select').value,'');
 assert.equal(candidates[0].querySelectorAll('select')[1].value,(await (await fetch(base+'/api/users')).json())[0].id);
 candidates[0].querySelector('button').click();
 await until(()=>d.getElementById('message').textContent.includes('登録してペアリングを開始しました'));
 const registered=(await (await fetch(base+'/api/devices')).json()).find(x=>x.address==='11:22:33:44:55:66');
 assert.equal(registered.model,'HBF-228T');assert.equal(registered.adapter,'hci1');
 assert.equal(registered.bindings['1'],(await (await fetch(base+'/api/users')).json())[0].id);
 assert.equal(registered.auto_sync,true);
 assert.equal(registered.sync_mode,'listen');
 assert.equal(d.querySelector('#coach-form [name=consent]'),null);
 assert(d.getElementById('ai-test'));
 assert.equal(d.getElementById('device-form').elements.sync_mode.value,'listen');
 const listenerFetch=w.fetch;
 const diagnosticJob={id:'diag-open',device_id:registered.id,action:'sync',state:'done',
  created_at:new Date().toISOString(),message:'待ち受け同期: 0件追加・30件重複・0件日時不正',
  result:JSON.stringify({diagnostic:{stage:'completed',listen:{advert_fingerprint:'abc123',advert_rssi:-60}}})};
 let diagnosticJobs=[diagnosticJob];
 w.fetch=(url,init)=>url==='/api/listener'?Promise.resolve(new Response(JSON.stringify({configured:true,ready:true,waiting:[{device_id:registered.id,attempt:2,seconds:42}],completed_today:[]}),{headers:{'Content-Type':'application/json'}})):url==='/api/jobs'&&(!init||!init.method)?Promise.resolve(new Response(JSON.stringify(diagnosticJobs),{headers:{'Content-Type':'application/json'}})):listenerFetch(url,init);
 await w.__testRefreshJobs();
 assert.match(d.getElementById('listen-status').textContent,/再試行まで約42秒/);
 assert.doesNotMatch(d.getElementById('listen-status').textContent,/本日の.*同期済み/);
 assert.match(d.getElementById('device-list').textContent,/待ち受け・通知ごと/);
 const diagnosticRow=d.querySelector('#jobs .job[data-job-id="diag-open"]');
 const diagnosticDetails=diagnosticRow.querySelector('details');
 diagnosticDetails.open=true;
 await w.__testRefreshJobs();
 assert.equal(d.querySelector('#jobs .job[data-job-id="diag-open"]'),diagnosticRow);
 assert.equal(d.querySelector('#jobs details'),diagnosticDetails);
 assert.equal(diagnosticDetails.open,true);
 assert.match(diagnosticDetails.querySelector('pre').textContent,/advert_fingerprint/);
 const newerJob={id:'diag-new',device_id:registered.id,action:'sync',state:'running',
  created_at:new Date(Date.now()+1000).toISOString(),message:'同期中',result:null};
 diagnosticJobs=[newerJob,diagnosticJob];
 await w.__testRefreshJobs();
 assert.equal(d.querySelector('#jobs .job').dataset.jobId,'diag-new');
 assert.equal(d.querySelector('#jobs .job[data-job-id="diag-open"]'),diagnosticRow);
 assert.equal(diagnosticDetails.open,true);
 diagnosticJob.message='待ち受け同期: 完了';
 await w.__testRefreshJobs();
 assert.equal(d.querySelector('#jobs .job[data-job-id="diag-open"]'),diagnosticRow);
 assert.equal(diagnosticDetails.open,true);
 assert.match(diagnosticRow.textContent,/完了/);
 w.fetch=listenerFetch;

 d.querySelector('[data-page="wellness"]').click();
 await until(()=>d.getElementById('wellness-summary').textContent.includes('直近の血圧'));
 assert.equal(d.querySelector('[data-panel="today"]').hidden,false);
 d.querySelector('[data-tabs="wellness"] [data-tab="coach"]').click();
 assert.equal(d.querySelector('[data-panel="today"]').hidden,true);assert.equal(d.querySelector('[data-panel="coach"]').hidden,false);
 assert.equal(d.querySelector('[data-tabs="wellness"] [data-tab="coach"]').getAttribute('aria-selected'),'true');
 assert(d.querySelectorAll('#coach-presets .chip-button').length>=3);
 d.querySelector('#coach-form [value="meal"]').click();d.getElementById('coach-form').dispatchEvent(new w.Event('change',{bubbles:true}));
 assert.match(d.getElementById('coach-presets').textContent,/外食/);
 {const bodies=[];const before=w.fetch;
  w.fetch=aiJobFetch(before,()=>'## 要点\n- **減塩**を続ける\n<img src=x onerror=alert(1)>');const jobBodies=w.fetch.bodies;
  d.getElementById('coach-form').elements.question.value='塩分を減らしたい';submit('coach-form');
  await until(()=>!d.getElementById('coach-answer').hidden);assert.equal(jobBodies[0].kind,'consult');
  assert.equal(d.querySelector('#coach-answer h4').textContent,'要点');
  assert.equal(d.querySelector('#coach-answer li strong').textContent,'減塩');
  assert.equal(d.querySelector('#coach-answer img'),null);
  d.getElementById('coach-form').elements.question.value='外食のときは？';submit('coach-form');
  await until(()=>jobBodies.length===2&&d.querySelectorAll('#coach-thread details').length===1);bodies.push(...jobBodies);
  assert.equal(bodies[0].mode,'meal');assert.equal(bodies[0].history,undefined);
  assert.equal(bodies[1].history[0].question,'塩分を減らしたい');
  d.getElementById('coach-reset').click();assert.equal(d.getElementById('coach-answer').hidden,true);
  w.fetch=before;}
 d.querySelector('[data-tabs="wellness"] [data-tab="metrics"]').click();
 await until(()=>d.querySelectorAll('#indicator-health .indicator-card').length>=5);
 assert.match(d.getElementById('indicator-health').textContent,/家庭血圧/);
 assert.match(d.getElementById('indicator-health').textContent,/何がわかるか/);
 await until(()=>d.getElementById('advice-settings-form').elements.time.value==='02:00');
 assert.equal(d.getElementById('advice-settings-form').elements.enabled.checked,true);
 const adviceForm=d.getElementById('advice-settings-form');adviceForm.elements.time.value='05:15';submit('advice-settings-form');
 await until(()=>d.getElementById('message').textContent==='毎日のアドバイスの設定を保存しました');
 assert.equal((await (await fetch(base+'/api/ai/daily-settings')).json()).time,'05:15');
 assert.match(d.getElementById('advice-latest').textContent,/まだアドバイスはありません/);
 assert(d.getElementById('ai-settings').closest('[data-panel="coach"]'));
 d.querySelector('[data-tabs="wellness"] [data-tab="today"]').click();
 const wellbeing=d.getElementById('wellness-form');wellbeing.elements.height_cm.value='180';wellbeing.elements.age.value='40';wellbeing.elements.sex.value='male';submit('wellness-form');
 await until(()=>d.getElementById('message').textContent==='健康設定と計画を保存しました');
 assert.equal((await (await fetch(base+'/api/wellness/profile?user_id='+d.getElementById('current-user').value)).json()).height_cm,180);
 d.querySelector('[data-page="settings"]').click();
 const legacy='"測定日","タイムゾーン","体重(kg)","体脂肪(%)","内臓脂肪レベル","基礎代謝(kcal)","骨格筋(%)","BMI","体年齢(才)","機種"\n"2021/07/13 19:06","Asia/Tokyo","106.60","28.4","17","2157","30.0","31.5","57","HBF-228T"\n';
 Object.defineProperty(d.getElementById('omron-file'),'files',{value:[{size:legacy.length,arrayBuffer:async()=>new TextEncoder().encode(legacy).buffer}],configurable:true});
 submit('omron-history-form');
 await until(()=>d.getElementById('omron-preview').textContent.includes('追加予定1件'));
 assert.equal(d.getElementById('omron-apply').disabled,false);
 d.getElementById('omron-apply').click();
 await until(()=>d.getElementById('message').textContent.includes('過去データを反映しました'));
 const imported=(await (await fetch(base+'/api/records')).json()).records.find(r=>r.kind==='body_composition');
 assert.equal(imported.values.weight,106.6);
 await until(()=>d.getElementById('latest-bmi').textContent==='31.5');
 assert.equal(d.getElementById('latest-body-fat').textContent,'28.4');
 assert.equal(d.getElementById('latest-muscle').textContent,'30.0');
 assert.equal(d.getElementById('latest-visceral-fat').textContent,'17');
 assert.match(d.querySelector('.body-summary-card').textContent,/内臓脂肪/);
 assert.equal(d.querySelector('.body-summary-card').textContent.includes('内臓脂肪レベル'),false);
 const bodySummaryCss=fs.readFileSync('web/styles.css','utf8');
 assert.match(bodySummaryCss,/\.body-summary-card>\.body-summary strong>span\{font-size:inherit\}/);
 assert.equal(d.querySelector('.body-summary-card').textContent.includes('DOM Test'),false);
 assert.equal(d.querySelector('.body-summary-card').textContent.includes('2021'),false);
 await w.fetch('/api/records',{method:'POST',headers:{'Content-Type':'application/json','X-SelfCare-Request':'1'},body:JSON.stringify({user_id:d.getElementById('current-user').value,kind:'body_composition',measured_at:'2026-09-25T10:00:00Z',values:{weight:81}})});
 d.getElementById('since').value='2026-09-01';
 d.getElementById('until').value='';
 d.getElementById('apply-filter').click();
 await until(()=>d.getElementById('latest-bmi').textContent==='25.0');
 assert.deepEqual(JSON.parse(w.localStorage.getItem('qnap-selfcare-overview-period-v1')),{since:'2026-09-01',until:''});
 d.getElementById('since').value='';d.getElementById('until').value='';
 w.__testRestorePeriod();
 assert.equal(d.getElementById('since').value,'2026-09-01');
 assert.equal(d.getElementById('until').value,'');
 d.querySelector('[data-page="wellness"]').click();
 await until(()=>d.getElementById('energy-today').textContent.includes('1740'));
 const activity=d.getElementById('activity-form');activity.elements.date.value='2026-09-25';activity.elements.pal.value='1.5';activity.elements.steps.value='8000';activity.elements.total_kcal.value='2500';activity.elements.intake_kcal.value='2100';submit('activity-form');
 await until(()=>d.getElementById('message').textContent==='活動記録を保存しました');
 assert.match(d.getElementById('activity-list').textContent,/-400 kcal/);
 assert(d.querySelector('#energy-chart svg'));
 assert(d.querySelector('#energy-chart svg.chart-cursor-svg'));
 assert.equal(d.querySelector('#energy-chart svg').getAttribute('tabindex'),'0');
 const energyUrl='/api/wellness/energy?user_id='+d.getElementById('current-user').value;
 let energyState=await (await fetch(base+energyUrl)).json();
 assert.equal(energyState.routine.intake_kcal,2100);
 activity.elements.reuse.value='once';activity.elements.intake_kcal.value='2300';submit('activity-form');
 await until(async()=>((await (await fetch(base+energyUrl)).json()).current.intake_kcal===2300));
 assert.equal((await (await fetch(base+energyUrl)).json()).routine.intake_kcal,2100);
 await until(()=>d.getElementById('energy-today').textContent.includes('2300'));
 d.getElementById('activity-previous').click();
 assert.equal(activity.elements.intake_kcal.value,'2300');
 d.getElementById('routine-stop').click();
 await until(()=>d.getElementById('routine-stop').hidden);
 energyState=await (await fetch(base+energyUrl)).json();
 assert.equal(energyState.routine,null);
 assert.equal(energyState.current.intake_kcal,2300);

 const plan=d.getElementById('weight-plan-form');plan.elements.start_date.value='2026-09-25';plan.elements.start_weight.value='81';plan.elements.goal_weight.value='76';plan.elements.goal_date.value='2027-03-01';submit('weight-plan-form');
 await until(()=>d.getElementById('message').textContent==='減量計画と見直し履歴を保存しました');
 assert.match(d.getElementById('weekly-review').textContent,/目標まで 5 kg/);
 assert.equal(wellbeing.elements.goal_date.disabled,true);
 assert.match(d.getElementById('weight-plan-chart').textContent,/代謝変化を含む試算/);
 assert.match(d.getElementById('plan-analysis').textContent,/目標日の試算体重/);
 d.getElementById('plan-info').click();assert.equal(d.getElementById('plan-explanation').hidden,false);assert.match(d.getElementById('plan-explanation').textContent,/2本の線は意味が違います/);
 assert.match(d.getElementById('plan-calc-preview').textContent,/基礎代謝/);
 const aiFetch=w.fetch;
 w.fetch=aiJobFetch(aiFetch,b=>b.kind==='meal'?'朝食: パン 300 kcal、卵 150 kcal。小計 450 kcal。\n昼食: 700 kcal。\n夕食: 700 kcal。\n合計 1850 kcal。':'期間・標準摂取・生活活動・標準運動を比較して見直します。');
 const meal=d.getElementById('meal-estimate-form');meal.elements.date.value='2026-09-25';meal.elements.breakfast.value='パンと卵';meal.elements.lunch.value='カレー';meal.elements.dinner.value='焼き魚定食';submit('meal-estimate-form');
 await until(()=>d.getElementById('meal-estimate-summary').textContent.includes('1850 kcal'));
 assert.match(d.getElementById('meal-estimate-summary').textContent,/パン 300 kcal.*卵 150 kcal/);
 assert.equal(d.getElementById('meal-to-daily'),null);assert.equal(d.getElementById('meal-to-standard'),null);
 assert.notEqual(activity.elements.intake_kcal.value,'1850');assert.notEqual(plan.elements.target_kcal.value,'1850');
 d.getElementById('plan-ai').click();await until(()=>d.getElementById('plan-ai-answer').textContent.includes('標準運動'));
 w.fetch=aiFetch;
 // Long-running consultation (e.g. QnapAssistant loading a model for minutes): the browser only polls the job.
 const previousFetch=w.fetch;
 w.fetch=aiJobFetch(previousFetch,()=>'長時間のAI相談の結果',['loading_model','completed']);
 d.getElementById('coach-form').elements.question.value='睡眠と血圧について教えて';
 submit('coach-form');
 await until(()=>/^\{"id":"job_/.test(w.sessionStorage.getItem('selfcare-ai-job-v2')||''));
 await until(()=>d.getElementById('coach-progress').textContent.includes('モデルを読み込み中'));
 assert.match(d.getElementById('coach-progress').textContent,/6分40秒/);
 assert.match(d.getElementById('coach-progress').textContent,/8.0 GiB/);
 await until(()=>d.getElementById('coach-answer').textContent.includes('長時間のAI相談の結果'),200);
 assert.equal(w.sessionStorage.getItem('selfcare-ai-job-v2'),null);
 w.fetch=previousFetch;
 {const before=w.fetch;const applied=[];let confirms=0;const confirmBefore=w.confirm;w.confirm=()=>{confirms++;return true;};
  const proposal={id:7,status:'pending',trigger:'schedule',created_at:'2026-10-28T02:05:00+09:00',reasons:['計画線より2.2 kg重い状態です。'],recommended:'B',recommended_by:'ai',
   answer:'推奨: B\n## 理由\n- 期日を守れます',error:null,options:[{id:'A',kind:'extend',title:'目標日を見直す',changes:['目標日: 2027-01-14 → 2027-03-07'],warnings:[],weekly_loss_kg:0.4,weekly_pct:0.5,planned_intake_kcal:2096,projected_goal_weight:72.6,projected_gap_kg:0.6,entry:{goal_date:'2027-03-07'}},
   {id:'B',kind:'intake',title:'摂取量を見直す',changes:['標準摂取: 自動 → 1760 kcal/日'],warnings:[],weekly_loss_kg:0.66,weekly_pct:0.84,planned_intake_kcal:1760,projected_goal_weight:72.4,projected_gap_kg:0.4,entry:{goal_date:'2027-01-14'}}]};
  w.fetch=(url,init)=>{if(typeof url==='string'&&url.startsWith('/api/wellness/plan-proposals?'))return jsonResponse({pending:applied.length?null:proposal,history:[proposal],has_plan:true,checking:false});
   if(url==='/api/wellness/plan-proposals/apply'){applied.push(JSON.parse(init.body));return jsonResponse({applied:'B'});}return before(url,init);};
  await w.__testRefreshProposals();
  const box=d.getElementById('plan-proposal');
  assert.match(box.textContent,/計画線より2.2 kg重い/);assert.match(box.textContent,/AIのおすすめ/);
  assert.doesNotMatch(box.textContent,/推奨: B/);   // the machine-readable line is hidden
  assert.equal(box.querySelectorAll('.proposal-option').length,2);
  assert(box.querySelector('.proposal-option.recommended').textContent.includes('案B'));
  box.querySelector('.proposal-option.recommended button').click();
  await until(()=>applied.length===1);
  assert.equal(confirms,1);assert.deepEqual({...applied[0],user_id:undefined},{user_id:undefined,proposal_id:7,option:'B'});
  w.fetch=before;w.confirm=confirmBefore;}
 {const before=w.fetch;let cancelled=0;
  w.fetch=(url,init)=>url==='/api/ai/activity'?jsonResponse({busy:true,waiting_interactive:1,deadline_seconds:2700,stall_after_seconds:180,
    current:{label:'毎日のアドバイス（けんしろう・10/27分）',interactive:false,provider:'qnapassistant',stage:'loading_model',detail:'空きRAM 3.2 GiB',elapsed_seconds:1029,since_update_seconds:250,errors:2,stalled:true}})
   :url==='/api/ai/activity/cancel'?(cancelled++,jsonResponse({busy:false})):before(url,init);
  await w.__testRefreshAiActivity();
  const act=d.getElementById('ai-activity').textContent;
  assert.match(act,/毎日のアドバイス（けんしろう・10\/27分）（自動）/);assert.match(act,/モデルを読み込み中（17分9秒経過）/);
  assert.match(act,/4分10秒前（通信エラー 2回・再試行中）/);assert.match(act,/応答がありません/);assert.match(act,/約28分/);assert.match(act,/画面からの依頼 1件/);
  assert.equal(d.getElementById('ai-activity-cancel').hidden,false);
  d.getElementById('ai-activity-cancel').click();await until(()=>cancelled===1);
  assert.match(w.__testAiProgressText({stage:'waiting',elapsed_seconds:65,blocking:{label:'毎日のアドバイス（けんしろう・10/27分）',stage:'generating',elapsed_seconds:600,stalled:false}}),/「毎日のアドバイス（けんしろう・10\/27分）」の完了を待っています（その処理は回答を生成中・10分0秒経過）。待ち時間 1分5秒/);
  w.fetch=before;}
 plan.elements.goal_date.value='2027-04-01';plan.elements.reason.value='活動量に合わせ延長';submit('weight-plan-form');
 await until(()=>d.getElementById('plan-history').textContent.includes('活動量に合わせ延長'));
 assert.match(d.getElementById('plan-history').textContent,/2027-03-01/);
 assert.match(d.getElementById('plan-history').textContent,/2027-04-01/);
 d.getElementById('current-user').value='';
 d.getElementById('apply-filter').click();
 await until(()=>d.getElementById('latest-bmi').textContent==='—');
 console.log('UI records, discovery-based registration and OMRON history transfer: passed');
}finally{dom?.window.close();child.kill('SIGTERM');await new Promise(r=>child.once('exit',r));fs.rmSync(data,{recursive:true,force:true});}})().catch(e=>{console.error(e);process.exitCode=1;});
