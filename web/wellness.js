'use strict';
const WELLNESS_FIELDS=['height_cm','age','sex','goal_weight_kg','goal_date','bp_sys_low','bp_sys_high','bp_dia_low','bp_dia_high','bmi_low','bmi_high','plan_text','step_goal'];
async function refreshWellness(){
 const userId=$('current-user').value;
 resumeCoachJob();
 $('wellness-empty').hidden=!!userId;$('wellness-content').hidden=!userId;
 if(!userId){wellnessProfile=null;wellnessProfileUser=null;renderBmi();renderCharts();return;}
 const [summary,meals,energy]=await Promise.all([api('/api/wellness/summary?user_id='+encodeURIComponent(userId)),api('/api/wellness/meals?user_id='+encodeURIComponent(userId)),api('/api/wellness/energy?user_id='+encodeURIComponent(userId))]);
 if(userId!==$('current-user').value)return;
 wellnessProfile=summary.profile;wellnessProfileUser=userId;renderBmi();const form=$('wellness-form');
 for(const key of WELLNESS_FIELDS)form.elements[key].value=summary.profile[key]??'';
 renderCharts();renderEnergy(energy,summary);refreshIndicators(userId).catch(()=>{});refreshAdvice(userId).catch(()=>{});
 const rows={'現在の体重':summary.latest_weight==null?'—':summary.latest_weight+' kg',
  'BMI（身長から算出）':summary.bmi??'身長・体重を設定してください',
  '推定基礎代謝':summary.estimated_bmr_kcal==null?'身長・年齢・計算条件・体重を設定してください':summary.estimated_bmr_kcal+' kcal/日',
  '直近の血圧':summary.latest_pressure?`${summary.latest_pressure.systolic} / ${summary.latest_pressure.diastolic} mmHg`:'—',
  '7日以上の体重変化':summary.weight_change_7d_kg==null?'比較できる記録がありません':`${summary.weight_change_7d_kg>0?'+':''}${summary.weight_change_7d_kg} kg`,
  '目標':summary.goal_weight_kg==null?'未設定':`${summary.goal_weight_kg} kg${summary.profile.goal_date?'（'+summary.profile.goal_date+'まで）':''}`,
  '目標への週あたりの変化':summary.weekly_change_needed_kg==null?'—':`${summary.weekly_change_needed_kg} kg/週の減量`};
 $('wellness-summary').replaceChildren();for(const [label,value] of Object.entries(rows)){const item=element('div');item.append(element('dt',label),element('dd',String(value)));$('wellness-summary').append(item);}
 $('wellness-notices').replaceChildren();for(const notice of summary.notices)$('wellness-notices').append(element('p',notice,'notice'));if(!summary.notices.length)$('wellness-notices').append(element('p','現在の設定で追加の見直し通知はありません。','notice'));
 $('meal-list').replaceChildren();for(const m of meals.slice(0,20)){const row=element('div',undefined,'meal-row');row.append(element('span',dateText(m.created_at)+'　'+m.note+(m.calories===null?'':'　約'+m.calories+' kcal')));const remove=element('button','削除','danger');remove.type='button';remove.addEventListener('click',guarded(async()=>{if(!confirm('この食事メモを削除しますか？'))return;await api('/api/wellness/meals',{user_id:userId,id:m.id},'DELETE');await refreshWellness();message('食事メモを削除しました');}));const reuse=element('button','同じ内容を入力','secondary');reuse.type='button';reuse.onclick=()=>{const f=$('meal-form');f.elements.note.value=m.note;f.elements.calories.value=m.calories??'';f.elements.note.focus();};row.append(reuse,remove);$('meal-list').append(row);}
}
$('wellness-form').addEventListener('submit',guarded(async()=>{
 const user_id=$('current-user').value;if(!user_id)throw Error('利用者を選択してください');const f=$('wellness-form'),profile={};
 for(const key of WELLNESS_FIELDS){const v=f.elements[key].value;profile[key]=['height_cm','age','goal_weight_kg'].includes(key)?(v?Number(v):null):key.startsWith('bp_')||key.startsWith('bmi_')||key==='step_goal'?Number(v):v;}
 await api('/api/wellness/profile',{user_id,profile});await refreshWellness();message('健康設定と計画を保存しました');
}));
$('meal-form').addEventListener('submit',guarded(async()=>{
 const user_id=$('current-user').value;if(!user_id)throw Error('利用者を選択してください');const f=$('meal-form');await api('/api/wellness/meals',{user_id,meal:{note:f.elements.note.value,calories:f.elements.calories.value?Number(f.elements.calories.value):null}});f.reset();await refreshWellness();message('食事メモを保存しました');
}));
// Minimal, safe Markdown for AI answers: headings, lists, bold. Built from text nodes only (never innerHTML).
function renderMarkdown(target,text){
 target.replaceChildren();let list=null,para=null;
 const inline=(node,line)=>{line.split(/(\*\*[^*]+\*\*)/).forEach(part=>{if(/^\*\*[^*]+\*\*$/.test(part))node.append(element('strong',part.slice(2,-2)));else if(part)node.append(document.createTextNode(part));});};
 for(const raw of String(text).split('\n')){
  const line=raw.trimEnd();const heading=line.match(/^#{1,4}\s+(.*)$/),bullet=line.match(/^\s*(?:[-*・•]|\d+[.)])\s+(.*)$/);
  if(!line.trim()){list=null;para=null;continue;}
  if(heading){list=null;para=null;const h=element('h4');inline(h,heading[1]);target.append(h);continue;}
  if(bullet){para=null;if(!list){list=element(/^\s*\d/.test(line)?'ol':'ul');target.append(list);}const li=element('li');inline(li,bullet[1]);list.append(li);continue;}
  list=null;if(!para){para=element('p');target.append(para);}else para.append(element('br'));inline(para,line);
 }
}
const COACH_PRESETS={
 health:['この1週間の変化をまとめて','気になる数値はある？','次の受診で伝えるとよいことは？'],
 lifestyle:['歩数を無理なく増やすには？','睡眠のリズムを整えたい','運動を習慣にするコツは？'],
 meal:['最近の食事の良い点と改善点は？','夕食の量を減らす工夫は？','外食が多い日の選び方は？'],
 diet:['今の目標は無理がない？','停滞しているときの見直し方は？','目標日までのペースは妥当？'],
 review:['計画どおりに進んでいる？','計画を見直すなら何を変える？','摂取量と運動、どちらを調整すべき？']};
let coachTurns=[];
function coachMode(){return $('coach-form').elements.mode.value||'health';}
function renderCoachPresets(){
 const box=$('coach-presets');box.replaceChildren();
 for(const text of COACH_PRESETS[coachMode()]||[]){const b=element('button',text,'chip-button');b.type='button';b.addEventListener('click',()=>{const q=$('coach-form').elements.question;q.value=text;q.focus();});box.append(b);}
}
function renderCoachThread(){
 const thread=$('coach-thread');thread.replaceChildren();
 coachTurns.slice(0,-1).forEach((turn,i)=>{const d=element('details',undefined,'coach-turn');d.append(element('summary',`${i+1}. ${turn.question}`));const body=element('div',undefined,'ai-answer');renderMarkdown(body,turn.answer);d.append(body);thread.append(d);});
}
function showCoachAnswer(question,answer){
 coachTurns.push({question:question||'（再接続した相談）',answer});coachTurns=coachTurns.slice(-4);
 renderCoachThread();const box=$('coach-answer');renderMarkdown(box,answer);
 const q=element('p','Q. '+(question||'（再接続した相談）'),'coach-question');box.prepend(q);
 box.hidden=false;$('coach-actions').hidden=false;$('coach-plan').hidden=false;$('coach-reset').hidden=false;
 const f=$('coach-form');f.elements.question.value='';f.elements.question.placeholder='続けて質問できます（直前のやり取りも送ります）';
 box.scrollIntoView({block:'start',behavior:'smooth'});
}
function resetCoach(){
 coachTurns=[];renderCoachThread();$('coach-answer').hidden=true;$('coach-actions').hidden=true;$('coach-reset').hidden=true;
 const q=$('coach-form').elements.question;q.placeholder='例: この一週間の変化を踏まえて食事の工夫を考えたい';
}
$('coach-form').addEventListener('change',e=>{if(e.target.name==='mode')renderCoachPresets();});
$('coach-reset').addEventListener('click',resetCoach);
$('coach-copy').addEventListener('click',guarded(async()=>{const text=coachTurns.at(-1)?.answer||'';try{await navigator.clipboard.writeText(text);message('回答をコピーしました');}catch{throw Error('コピーできませんでした。回答を長押しして選択してください');}}));
renderCoachPresets();
// The async QnapAssistant job ID is a short-lived capability. Store only the
// opaque ID (never health inputs/responses) so a page reload can resume polling.
const coachJobKey='selfcare-ai-consult-job-v1';
let coachActiveJob=null,coachPollTimer=null,coachPollFailures=0;
const coachTerminal=new Set(['completed','failed','timed_out','cancelled']);
function coachProgress(title,detail){
 $('coach-progress').hidden=false;$('coach-progress-title').textContent=title;
 $('coach-progress-text').textContent=detail;
 $('coach-cancel').hidden=!coachActiveJob;
}
function clearCoachJob(){
 if(coachPollTimer!==null){clearTimeout(coachPollTimer);coachPollTimer=null;}
 coachActiveJob=null;coachPollFailures=0;sessionStorage.removeItem(coachJobKey);
 $('coach-cancel').hidden=true;
}
function scheduleCoachPoll(delay=2500){
 if(coachPollTimer!==null)clearTimeout(coachPollTimer);
 coachPollTimer=setTimeout(()=>{coachPollTimer=null;pollCoachJob().catch(e=>coachProgress('状態の確認に失敗',e.message));},delay);
}
async function pollCoachJob(){
 const current=coachActiveJob;if(!current)return;
 if($('current-user').value!==current.user_id){
  coachProgress('利用者を確認してください','別の利用者が選択されています。受付ID '+current.id+' の相談結果は表示しません。');
  scheduleCoachPoll(4000);return;
 }
 let d;
 try{
  d=await api('/api/ai/consult/status?request_id='+encodeURIComponent(current.id));
  coachPollFailures=0;
 }catch(e){
  if(coachActiveJob!==current)return;
  coachPollFailures++;
  if(/受付IDが見つかりません/.test(e.message)){
   coachProgress('処理の復旧が必要です','QnapAssistantが再起動したか、受付履歴の保存期限が切れました。'+e.message);clearCoachJob();return;
  }
  coachProgress('通信状態を確認中','受付ID '+current.id+' ／ 状態の照会に失敗（'+coachPollFailures+'回）。次回も再確認します。'+e.message);
  scheduleCoachPoll(Math.min(10000,2000+coachPollFailures*1000));return;
 }
 if(coachActiveJob!==current)return;
 const names={queued:'受付済み・待機中',loading_model:'ローカルモデルのロード中',ready:'推論準備完了',generating:'回答を生成中',completed:'回答生成完了',failed:'処理失敗',timed_out:'処理タイムアウト',cancelled:'キャンセル済み'};
 const available=Number.isFinite(Number(d.memory_available_bytes))? ' ／ 空きRAM '+(Number(d.memory_available_bytes)/1073741824).toFixed(2)+' GiB':'';
 coachProgress(names[d.status]||'状態確認中','受付ID '+current.id+' ／ 経過 '+(d.elapsed_seconds??'?')+'秒 ／ 段階 '+(d.stage_elapsed_seconds??'?')+'秒'+available+(d.error?' ／ '+d.error:''));
 if(d.status==='completed'){
  try{
   const result=await api('/api/ai/consult/result?request_id='+encodeURIComponent(current.id));
   if(coachActiveJob!==current||$('current-user').value!==current.user_id)return;
   showCoachAnswer(current.question,result.answer);
   coachProgress('回答を取得しました','受付ID '+current.id+' ／ 処理完了');clearCoachJob();
   message('相談への回答を受け取りました');
  }catch(e){coachProgress('回答の取得を再試行中',e.message);scheduleCoachPoll(3500);}
 }else if(coachTerminal.has(d.status)){
  coachProgress(names[d.status]||'処理終了','受付ID '+current.id+' ／ '+(d.error||'処理は完了しませんでした'));
  clearCoachJob();
 }else{
  scheduleCoachPoll(2500);
 }
}
function resumeCoachJob(){
 if(coachActiveJob)return;
 let saved;try{saved=JSON.parse(sessionStorage.getItem(coachJobKey)||'null');}catch{return;}
 if(!saved||!/^req_[0-9a-f]{48}$/.test(saved.id)||typeof saved.user_id!=='string')return;
 if(!$('current-user').value||$('current-user').value!==saved.user_id)return;
 coachActiveJob=saved;coachProgress('相談を再接続中','受付ID '+saved.id+' の処理状況を確認します。');scheduleCoachPoll(0);
}
$('coach-form').addEventListener('submit',guarded(async()=>{
 const user_id=$('current-user').value;if(!user_id)throw Error('利用者を選択してください');
 if(coachActiveJob)throw Error('相談処理が進行中です。完了またはキャンセル後に再実行してください');
 const f=$('coach-form'),params={user_id,mode:coachMode(),question:f.elements.question.value};
 if(coachTurns.length)params.history=coachTurns.slice(-3).map(t=>({question:t.question,answer:t.answer.slice(0,4000)}));
 if($('ai-form').elements.provider.value!=='qnapassistant'){
  coachProgress('同期APIで相談中','現在の相談先では従来の同期方式で実行します。');
  const result=await api('/api/ai/consult',params);
  showCoachAnswer(params.question,result.answer);
  $('coach-progress').hidden=true;message('相談への回答を受け取りました');return;
 }
 coachProgress('相談を受付中','QnapAssistantへ安全に相談を送信しています…');
 const accepted=await api('/api/ai/consult/start',params);
 coachActiveJob={id:accepted.request_id,user_id,question:params.question};
 // Only the opaque ID and user are persisted; the question stays in memory.
 sessionStorage.setItem(coachJobKey,JSON.stringify({id:coachActiveJob.id,user_id}));
 coachProgress('受付済み','受付ID '+coachActiveJob.id+' ／ 状況を確認しています…');
 scheduleCoachPoll(0);
}));
$('coach-cancel').addEventListener('click',guarded(async()=>{
 if(!coachActiveJob)return;
 const current=coachActiveJob;
 await api('/api/ai/consult/cancel',{request_id:current.id});
 if(coachActiveJob===current){coachProgress('キャンセル済み','受付ID '+current.id);clearCoachJob();}
}));
$('coach-plan').addEventListener('click',()=>{const field=$('wellness-form').elements.plan_text;field.value=(coachTurns.at(-1)?.answer||'').slice(0,8000);selectTab('wellness','profile');field.focus();message('計画欄へコピーしました。内容を確認して保存してください');});
async function refreshAiSettings(){const config=await api('/api/ai/settings');const f=$('ai-form');for(const key of ['provider','base_url','model'])f.elements[key].value=config[key];$('ai-status').textContent=config.key_configured?'APIキーを保存済み':'この接続先のAPIキーは未設定です';$('coach-provider').textContent='相談先: '+({controldeck:'ControlDeck',qnapassistant:'QnapAssistant',local:'ローカルLLM',openrouter:'OpenRouter'}[config.provider]||config.provider)+(config.provider==='openrouter'?'（外部サービス）':'（NAS・LAN内）');refreshAdviceSettings().catch(()=>{});}
// ---- 評価指標 tab ----
const STATUS_WORD={good:'良好',ok:'注意',warn:'要見直し',unknown:'データ不足'},CONFIDENCE_WORD={high:'信頼度 高',medium:'信頼度 中',low:'信頼度 低'};
async function refreshIndicators(userId){
 const data=await api('/api/wellness/metrics?user_id='+encodeURIComponent(userId));if(userId!==$('current-user').value)return;
 $('indicator-date').textContent=data.date.replaceAll('-','/')+' 時点';
 for(const [group,target] of [['diet','indicator-diet'],['health','indicator-health']]){
  $(target).replaceChildren(...data[group].map(m=>{
   const card=element('article',undefined,'indicator-card '+m.status);const head=element('div',undefined,'indicator-head');
   head.append(element('span',STATUS_WORD[m.status]||'','eval-badge '+m.status),element('h3',m.label),element('strong',m.value,'indicator-value'));
   card.append(head,element('p',m.detail,'indicator-detail'));
   if(m.explain){const ex=element('dl',undefined,'indicator-explain');ex.append(element('dt','何がわかるか'),element('dd',m.explain.what),element('dt','なぜ大切か'),element('dd',m.explain.why));card.append(ex);
    const more=element('details',undefined,'indicator-more');more.append(element('summary','計算方法と注意'));const how=element('dl',undefined,'indicator-explain');how.append(element('dt','計算方法'),element('dd',m.explain.how),element('dt','注意'),element('dd',m.explain.caveat));more.append(how);card.append(more);}
   const meta=element('p',undefined,'indicator-meta');meta.append(element('span','目安：'+m.reference));if(CONFIDENCE_WORD[m.confidence])meta.append(element('span',CONFIDENCE_WORD[m.confidence],'eval-confidence '+m.confidence));card.append(meta);
   return card;}));
 }
}
// ---- 毎日のアドバイス ----
const ADVICE_STATUS={done:'作成済み',running:'作成中…',retry:'AIが混雑していたため、少し後に再試行します',failed:'作成できませんでした',skipped:'記録がないため作成していません',review_only:'記録の比較のみ（AIなし）'};
let advicePoll=null;
function adviceDay(day){const d=new Date(day+'T12:00:00');return `${d.getMonth()+1}月${d.getDate()}日（${'日月火水木金土'[d.getDay()]}）`;}
function renderReview(review){
 const table=element('div',undefined,'review-grid');
 for(const i of review?.items||[]){if(i.value===null&&i.avg7===null)continue;
  const cell=element('div',undefined,'review-item '+(i.status||''));const arrow={up:'↑',down:'↓',usual:'→',none:''}[i.trend]||'';
  cell.append(element('span',i.label),element('strong',i.value===null?'—':`${i.value.toLocaleString('ja-JP')} ${i.unit}`),element('small',i.avg7===null?'7日平均なし':`${arrow} 7日平均 ${i.avg7.toLocaleString('ja-JP')}`));table.append(cell);}
 return table;
}
function renderAdviceEntry(target,item){
 target.replaceChildren();if(!item){target.append(element('p','まだアドバイスはありません。「昨日分を今すぐ作成」で作成できます。','muted'));return;}
 const head=element('div',undefined,'advice-head');head.append(element('h3',adviceDay(item.day)+'の振り返り'),element('span',ADVICE_STATUS[item.status]||item.status,'eval-badge '+({done:'good',running:'ok',retry:'ok',failed:'warn'}[item.status]||'')));target.append(head);
 if(item.review){target.append(renderReview(item.review));if(item.review.notes?.length){const ul=element('ul',undefined,'advice-notes');item.review.notes.forEach(n=>ul.append(element('li',n)));target.append(ul);}}
 if(item.answer){const box=element('div',undefined,'ai-answer');renderMarkdown(box,item.answer);target.append(box);}
 if(item.error)target.append(element('p',item.error,'notice'+(item.status==='failed'?' error':'')));
 target.append(element('small',`更新 ${new Date(item.updated_at).toLocaleString('ja-JP')}${item.trigger==='schedule'?'・自動作成':'・手動作成'}`,'muted'));
}
async function refreshAdvice(userId=$('current-user').value){
 if(!userId)return;const data=await api('/api/wellness/advice?user_id='+encodeURIComponent(userId));if(userId!==$('current-user').value)return;
 renderAdviceEntry($('advice-latest'),data.latest);
 const hist=$('advice-history');hist.replaceChildren();
 for(const item of data.history.slice(1)){const d=element('details',undefined,'coach-turn');d.append(element('summary',`${adviceDay(item.day)}　${ADVICE_STATUS[item.status]||item.status}`));const body=element('div');renderAdviceEntry(body,item);d.append(body);hist.append(d);}
 if(data.history.length<=1)hist.append(element('p','過去のアドバイスはありません。','muted'));
 renderAdviceSchedule(data.settings);
 const busy=data.settings.running||data.latest?.status==='running';
 $('advice-run-yesterday').disabled=busy;$('advice-run-today').disabled=busy;
 if(advicePoll)clearTimeout(advicePoll);advicePoll=busy?setTimeout(()=>refreshAdvice(userId).catch(()=>{}),5000):null;
}
function renderAdviceSchedule(cfg){
 const next=new Date(cfg.next_run);
 $('advice-schedule-status').textContent=cfg.enabled?`次回 ${next.getMonth()+1}/${next.getDate()} ${cfg.time}（${adviceDay(cfg.reviews_day)}分）`:'自動作成はオフ';
 const f=$('advice-settings-form');if(document.activeElement?.form!==f){f.elements.enabled.checked=cfg.enabled;f.elements.time.value=cfg.time;f.elements.allow_external.checked=cfg.allow_external;}
 $('advice-external-row').hidden=cfg.provider!=='openrouter';
 $('advice-settings-status').textContent=cfg.ai_blocked||'';
}
async function refreshAdviceSettings(){renderAdviceSchedule(await api('/api/ai/daily-settings'));}
$('advice-settings-form').addEventListener('submit',guarded(async()=>{const f=$('advice-settings-form');renderAdviceSchedule(await api('/api/ai/daily-settings',{enabled:f.elements.enabled.checked,time:f.elements.time.value,allow_external:f.elements.allow_external.checked}));message('毎日のアドバイスの設定を保存しました');}));
for(const [id,day] of [['advice-run-yesterday','yesterday'],['advice-run-today','today']])$(id).addEventListener('click',guarded(async()=>{const user_id=$('current-user').value;if(!user_id)throw Error('利用者を選択してください');await api('/api/wellness/advice/run',{user_id,day});message('アドバイスの作成を開始しました。完了まで数分かかることがあります');await refreshAdvice(user_id);}));
$('ai-form').addEventListener('submit',guarded(async()=>{const f=$('ai-form');await api('/api/ai/settings',{provider:f.elements.provider.value,base_url:f.elements.base_url.value.trim(),model:f.elements.model.value.trim()});await refreshAiSettings();message('AIの接続先を保存しました');}));
$('ai-key-form').addEventListener('submit',guarded(async()=>{const f=$('ai-key-form');await api('/api/ai/key',{provider:$('ai-form').elements.provider.value,key:f.elements.key.value});f.reset();await refreshAiSettings();message('APIキーを更新しました');}));
$('ai-form').elements.provider.addEventListener('change',()=>{const f=$('ai-form');const p=f.elements.provider.value;if(p==='qnapassistant')f.elements.base_url.value='http://127.0.0.1:11435/v1';else if(p==='openrouter')f.elements.base_url.value='https://openrouter.ai/api/v1';else f.elements.base_url.value='';f.elements.model.value=p==='openrouter'?'openai/gpt-4o-mini':'auto';});
refreshAiSettings().catch(e=>message(e.message,true));

$('ai-test').addEventListener('click',guarded(async()=>{
 $('ai-test-result').textContent='接続確認中…（最大5分）';
 try{const r=await api('/api/ai/test',{});$('ai-test-result').textContent='接続成功：'+r.answer;}
 catch(error){$('ai-test-result').textContent=error.message;throw error;}
}));

let energyData=null,wellnessSummaryData=null;
const activityNumbers=['pal','steps','minutes','total_kcal','intake_kcal','target_kcal'];
const planNumbers=['start_weight','goal_weight','pal','deficit_kcal','target_kcal','adaptation_pct','exercise_minutes','exercise_met'];
const energyValue=(v,unit='')=>v==null?'—':v+unit;
function energyPlot(id,series,unit){
 const container=$(id);container.replaceChildren();
 const points=series.flatMap(s=>s.points).filter(p=>p.value!=null&&Number.isFinite(p.value));
 if(!points.length){container.append(element('p','表示できる記録がありません。','muted'));return;}
 const ns='http://www.w3.org/2000/svg';const svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 480 230');svg.setAttribute('role','img');svg.setAttribute('aria-label',series.map(s=>s.name).join('・')+'（'+unit+'）');
 const make=(tag,attrs,text)=>{const n=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;svg.append(n);return n;};
 const times=points.map(p=>Date.parse(p.date+'T00:00:00Z')),lo=points.reduce((a,p)=>Math.min(a,p.value),Infinity),hi=points.reduce((a,p)=>Math.max(a,p.value),-Infinity);
 const xmin=times.reduce((a,t)=>Math.min(a,t),Infinity),xmax=times.reduce((a,t)=>Math.max(a,t),-Infinity),pad=Math.max((hi-lo)*.1,unit==='kg'?1:100),min=lo-pad,max=hi+pad;
 const x=d=>52+(Date.parse(d+'T00:00:00Z')-xmin)/Math.max(86400000,xmax-xmin)*414,y=v=>190-(v-min)/(max-min)*170;
 for(let i=0;i<4;i++){const v=min+(max-min)*i/3;make('line',{x1:52,x2:466,y1:y(v),y2:y(v),stroke:'currentColor',opacity:.15});make('text',{x:46,y:y(v)+4,'text-anchor':'end',fill:'currentColor','font-size':12},unit==='kg'?v.toFixed(1):Math.round(v));}
 for(const s of series){const usable=s.points.filter(p=>p.value!=null&&Number.isFinite(p.value));if(!usable.length)continue;
 make('polyline',{points:usable.map(p=>x(p.date)+','+y(p.value)).join(' '),fill:'none',stroke:s.color,'stroke-width':2,'stroke-dasharray':s.dash?'5 4':'none'});
 for(const p of usable.filter((_,i)=>i%Math.max(1,Math.ceil(usable.length/400))===0)){const n=make('circle',{cx:x(p.date),cy:y(p.value),r:2.5,fill:s.color});const t=document.createElementNS(ns,'title');t.textContent=p.date+' '+s.name+' '+p.value+' '+unit;n.append(t);}}
 make('text',{x:52,y:217,fill:'currentColor','font-size':12},new Date(xmin).toISOString().slice(0,10));make('text',{x:466,y:217,'text-anchor':'end',fill:'currentColor','font-size':12},new Date(xmax).toISOString().slice(0,10));container.append(svg);
 const cursorDates=new Map();
 for(const s of series){for(const p of s.points){if(p.value==null||!Number.isFinite(p.value))continue;
  if(!cursorDates.has(p.date))cursorDates.set(p.date,{x:x(p.date),label:p.date,fields:[]});
  cursorDates.get(p.date).fields.push({label:s.name,value:p.value,unit,y:y(p.value),color:s.color});
 }}
 window.attachChartCursor(container,svg,[...cursorDates.values()],{viewWidth:480,top:20,bottom:190});
 const legend=element('div',undefined,'energy-legend');for(const s of series){const item=element('span',(s.dash?'┄ ':'● ')+s.name);item.style.color=s.color;legend.append(item);}container.append(legend);
}
function drawEnergy(){
 if(!energyData)return;const data=energyData,range=$('energy-range').value,cutoff=range==='all'?'':new Date(Date.parse(data.today+'T00:00:00Z')-Number(range)*86400000).toISOString().slice(0,10);
 const h=data.history.filter(x=>x.date>=cutoff);energyPlot('energy-chart',[
 {name:'推定基礎代謝',color:'var(--chart-primary)',key:'bmr'},{name:'総消費',color:'var(--chart-secondary)',key:'total_kcal'},
 {name:'摂取（その日の入力）',color:'var(--energy-intake)',key:'intake_kcal',source:'entered'},
 {name:'摂取（いつもの設定）',color:'var(--energy-intake)',key:'intake_kcal',source:'routine',dash:true},{name:'摂取目標',color:'var(--energy-target)',key:'target_kcal',dash:true}
 ].map(s=>({...s,points:h.map(x=>({date:x.date,value:!s.source||x.source===s.source?x[s.key]:null}))})),'kcal/日');
 const p=data.active_plan;const series=[{name:'実測（日平均）',color:'var(--chart-primary)',points:h.filter(x=>x.weight!=null).map(x=>({date:x.date,value:x.weight}))}];
 if(p){series.push({name:'目標の計画線',color:'var(--energy-target)',dash:true,points:[{date:p.start_date,value:p.start_weight},{date:p.goal_date,value:p.goal_weight}]},{name:'代謝変化を含む試算',color:'var(--chart-secondary)',dash:true,points:data.projection.map(x=>({date:x.date,value:x.weight}))});}
 energyPlot('weight-plan-chart',series,'kg');
}
function fillActivity(entry){const f=$('activity-form');f.reset();f.elements.date.value=entry.date;for(const k of [...activityNumbers,'note'])if(entry[k]!=null){if(k==='pal'&&![...f.elements.pal.options].some(o=>o.value===String(entry[k])))f.elements.pal.add(new Option('設定値（'+entry[k]+'）',String(entry[k])));f.elements[k].value=entry[k];}}
function ensureOption(select,value,label='設定値'){
 if(value==null||value==='')return;
 if(![...select.options].some(o=>o.value===String(value)))select.add(new Option(label+'（'+value+'）',String(value)));
}
function planBmr(weight){
 const p=wellnessSummaryData?.profile;if(!p||!weight||!p.height_cm||p.age==null||!p.sex)return null;
 return Math.round(10*weight+6.25*p.height_cm-5*p.age+(p.sex==='male'?5:-161));
}
function planFormValues(){
 const f=$('weight-plan-form'),n=k=>f.elements[k].value===''?null:Number(f.elements[k].value);
 return {start_date:f.elements.start_date.value,goal_date:f.elements.goal_date.value,start_weight:n('start_weight'),goal_weight:n('goal_weight'),
  pal:n('pal'),deficit_kcal:n('deficit_kcal'),target_kcal:n('target_kcal'),adaptation_pct:n('adaptation_pct'),
  exercise_minutes:n('exercise_minutes'),exercise_met:n('exercise_met')};
}
function updatePlanPreview(){
 const box=$('plan-calc-preview');if(!box)return;const v=planFormValues(),basal=planBmr(v.start_weight);
 if(basal==null){box.textContent='身体情報の身長・年齢・基礎代謝の計算条件と開始体重を設定すると、ここに標準モデルの計算内訳を表示します。';return;}
 const exercise=Math.round(Math.max((v.exercise_met??1)-1,0)*3.5*v.start_weight/200*(v.exercise_minutes??0));
 const total=Math.round(basal*(v.pal??1.2)+exercise),floor=Math.max(1200,basal);
 const intake=v.target_kcal??Math.max(floor,Math.round(total-(v.deficit_kcal??0))),initial=Math.round(total-intake);
 let needed=null,neededIntake=null;
 if(v.start_date&&v.goal_date&&v.goal_weight!=null){const days=Math.round((Date.parse(v.goal_date+'T00:00:00Z')-Date.parse(v.start_date+'T00:00:00Z'))/86400000);if(days>0){needed=Math.round((v.start_weight-v.goal_weight)*7700/days);neededIntake=Math.round(total-needed);}}
 let text=`開始時の推定：基礎代謝 ${basal} kcal/日、生活活動＋標準運動の総消費 ${total} kcal/日（運動の追加分 約${exercise} kcal/日）、標準摂取 ${intake} kcal/日、初期の差 約${initial} kcal/日。`;
 if(needed!=null)text+=` 目標日から単純逆算すると平均 約${needed} kcal/日の差が必要です。`;
 if(neededIntake!=null&&neededIntake<floor)text+=` 逆算した開始時摂取 ${neededIntake} kcal/日は自動計算の下限 ${floor} kcal/日を下回るため、期間や活動量も含めて見直してください。`;
 box.textContent=text;
}
function renderPlanAnalysis(data){
 const box=$('plan-analysis');box.replaceChildren();const a=data.plan_analysis;
 if(!a){box.append(element('p','計画を保存すると、試算の到達点と目標との差を表示します。','muted'));return;}
 const gap=a.projected_gap_kg==null?'—':(a.projected_gap_kg>0?'+':'')+a.projected_gap_kg+' kg';
 const grid=element('div',undefined,'plan-analysis-grid');
 for(const [label,value] of [['目標日の試算体重',energyValue(a.projected_goal_weight,' kg')],['目標との差',gap],
  ['必要な平均エネルギー差',energyValue(a.required_daily_deficit_kcal,' kcal/日')],['開始時の推定総消費',energyValue(a.start_total_kcal,' kcal/日')],
  ['標準摂取',energyValue(a.planned_intake_kcal,' kcal/日')],['標準運動の追加消費',energyValue(a.start_exercise_kcal,' kcal/日')]]){
   const cell=element('div');cell.append(element('span',label),element('strong',String(value)));grid.append(cell);
 }
 box.append(grid);
 if(a.floor_blocks_required_intake)box.append(element('p',`目標日から逆算した開始時摂取 ${a.required_start_intake_kcal} kcal/日は、自動計算の下限 ${a.safety_floor_kcal} kcal/日を下回ります。摂取だけで合わせず、目標日の延長・生活活動・標準運動を含めて見直してください。`,'notice'));
}
function renderEnergy(data,summary){
 energyData=data;wellnessSummaryData=summary;const current=data.current,stats=$('energy-today');stats.replaceChildren();
 for(const [label,value]of [['推定基礎代謝',current.bmr],['今日の総消費',current.total_kcal],['今日の摂取目標',current.target_kcal],['摂取（'+(current.source==='routine'?'いつもの設定':'その日の入力')+'）',current.intake_kcal]]){const cell=element('div');cell.append(element('span',label),element('strong',energyValue(value,' kcal')));stats.append(cell);}
 $('energy-basis').textContent='体重の基準日：'+(current.weight_date||'未記録')+'／活動係数 '+current.pal+'。履歴の推定は現在の身体情報で再計算します。入力のない日は活動係数による目安です。';
 $('energy-notices').replaceChildren(...data.notices.map(t=>element('p',t,'notice')));
 fillActivity((data.entries||data.diaries).find(x=>x.date===data.today)||{date:data.today,...(data.routine||{}),pal:current.pal});$('activity-status').textContent='同じ日の保存はその日の入力を更新します。いつもの設定の変更は今日以降の未入力日に反映します。';
 $('routine-status').textContent=data.routine?`いつもの設定：活動係数 ${data.routine.pal} ／ 摂取 ${energyValue(data.routine.intake_kcal,' kcal/日')}。未入力の日に自動で使います。`:'いつもの設定は未登録です。前回の入力を使って保存することもできます。';$('routine-stop').hidden=!data.routine;
 const list=$('activity-list');list.replaceChildren();for(const entry of [...(data.entries||data.diaries)].reverse().slice(0,90)){const row=element('div',undefined,'energy-log');const h=data.history.find(x=>x.date===entry.date);row.append(element('strong',entry.date+(entry.source==='routine'?'（いつもの設定）':'（その日の入力）')),element('span',`歩数 ${energyValue(entry.steps)} · 運動 ${energyValue(entry.minutes,'分')} · 総消費 ${energyValue(h?.total_kcal,' kcal')}（${h?.total_source||'—'}） · 摂取 ${energyValue(entry.intake_kcal,' kcal')} · 収支 ${energyValue(h?.balance_kcal,' kcal')}`),element('span',entry.note));
 const actions=element('div',undefined,'row');const edit=element('button','編集','secondary');edit.type='button';edit.onclick=()=>{fillActivity(entry);$('activity-form').elements.reuse.value='once';selectTab('wellness','today');$('activity-form').scrollIntoView({block:'center'});};const remove=element('button','削除','danger');remove.type='button';const userId=$('current-user').value;remove.onclick=guarded(async()=>{if(!confirm(entry.date+'の活動記録を削除しますか？'))return;await api('/api/wellness/activity',{user_id:userId,date:entry.date},'DELETE');await refreshWellness();message('その日の入力を削除しました。いつもの設定があればそれを表示します');});actions.append(edit);if(entry.source!=='routine')actions.append(remove);row.append(actions);list.append(row);}
 if(!(data.entries||data.diaries).length)list.append(element('p','活動記録はまだありません。','muted'));
 const p=data.active_plan,f=$('weight-plan-form');f.reset();f.elements.start_date.value=data.today;f.elements.start_weight.value=summary.latest_weight??'';
 if(p){ensureOption(f.elements.pal,p.pal,'保存済み');ensureOption(f.elements.exercise_met,p.exercise_met??1,'保存済み');for(const k of [...planNumbers,'goal_date'])f.elements[k].value=p[k]??(k==='exercise_minutes'?0:k==='exercise_met'?1:'');}
 else{f.elements.goal_weight.value=summary.goal_weight_kg??'';f.elements.goal_date.value=summary.profile.goal_date||'';const usualPal=data.routine?.pal??current.pal??1.2;ensureOption(f.elements.pal,usualPal,'いつもの活動');f.elements.pal.value=usualPal;f.elements.target_kcal.value=data.routine?.intake_kcal??'';}
 // Editing always makes a revision anchored in today's measurements, not a rewrite of an old plan.
 f.elements.start_date.value=data.today;f.elements.start_weight.value=summary.latest_weight??p?.start_weight??'';f.elements.reason.required=!!p;
 const mealForm=$('meal-estimate-form');if(!mealForm.elements.date.value)mealForm.elements.date.value=data.today;renderPlanAnalysis(data);updatePlanPreview();
 for(const k of ['goal_weight_kg','goal_date'])$('wellness-form').elements[k].disabled=!!p;
 const review=$('weekly-review');review.replaceChildren();const elapsed=data.weeks.filter(w=>!w.future),recent=elapsed.slice(-4);const makeWeek=w=>{const row=element('div',undefined,'energy-week');row.append(element('strong',w.date.slice(5)+'〜'+w.end.slice(5)+(w.partial?'（途中）':'')),element('span','平均 '+energyValue(w.actual,' kg')+' / 計画 '+w.planned+' kg'),element('span','目標まで '+energyValue(w.remaining,' kg')+' · 計画差 '+(w.deviation>0?'+':'')+energyValue(w.deviation,' kg')),element('small',w.days+'日分の測定'+(w.days<3?'・少数のため参考':'')));return row;};
 if(!p)review.append(element('p','目標体重と日付を設定すると、週ごとの差を確認できます。','muted'));
 for(const w of recent)review.append(makeWeek(w));if(data.weeks.length>recent.length){const more=element('details');more.append(element('summary','全週の計画と実績'));for(const w of data.weeks)more.append(makeWeek(w));review.append(more);}
 const history=$('plan-history');history.replaceChildren();for(const v of [...data.plans].reverse()){const row=element('div',undefined,'energy-log');row.append(element('strong',dateText(v.created_at)),element('span',`${v.start_date} ${v.start_weight} kg → ${v.goal_date} ${v.goal_weight} kg`),element('span',`生活活動 ${v.pal} · 標準運動 ${v.exercise_minutes??0}分/日（MET ${v.exercise_met??1}） · 摂取 ${v.target_kcal==null?'消費−'+v.deficit_kcal:v.target_kcal} kcal · 追加低下感度 ${v.adaptation_pct}%`),element('span',v.reason||'初回計画'));history.append(row);}drawEnergy();
}
$('energy-range').addEventListener('change',drawEnergy);
$('activity-load').addEventListener('click',()=>{if(!energyData)return;const date=$('activity-form').elements.date.value,entry=(energyData.entries||energyData.diaries).find(x=>x.date===date);fillActivity(entry||{date,pal:energyData.current.pal});$('activity-form').elements.reuse.value='once';$('activity-status').textContent=entry?(entry.source==='routine'?'いつもの設定を読み込みました':'保存済みの入力を読み込みました'):'この日は未記録です';});
$('activity-previous').addEventListener('click',()=>{if(!energyData)return;const entry=energyData.diaries.at(-1);if(!entry){$('activity-status').textContent='前回の入力はありません';return;}const date=$('activity-form').elements.date.value||energyData.today;fillActivity({...entry,date});$('activity-status').textContent='前回の入力を読み込みました。保存すると反映します。';});
$('routine-stop').addEventListener('click',guarded(async()=>{await api('/api/wellness/routine',{user_id:$('current-user').value,entry:null});await refreshWellness();message('今日からの使い回しを停止しました。その日の入力は残ります');}));
$('activity-form').addEventListener('submit',guarded(async()=>{const f=$('activity-form'),entry={date:f.elements.date.value,note:f.elements.note.value};for(const k of activityNumbers)entry[k]=f.elements[k].value===''?null:Number(f.elements[k].value);await api('/api/wellness/activity',{user_id:$('current-user').value,entry,reuse:f.elements.reuse.value==='repeat'});await refreshWellness();message('活動記録を保存しました');}));
$('weight-plan-form').addEventListener('submit',guarded(async()=>{const f=$('weight-plan-form'),entry={start_date:f.elements.start_date.value,goal_date:f.elements.goal_date.value,reason:f.elements.reason.value};for(const k of planNumbers)entry[k]=f.elements[k].value===''?null:Number(f.elements[k].value);await api('/api/wellness/weight-plan',{user_id:$('current-user').value,entry});await refreshWellness();message('減量計画と見直し履歴を保存しました');}));
$('weight-plan-form').addEventListener('input',updatePlanPreview);$('weight-plan-form').addEventListener('change',updatePlanPreview);
$('plan-rebase').addEventListener('click',()=>{if(!energyData)return;const f=$('weight-plan-form'),weights=energyData.history.filter(x=>x.weight!=null);f.elements.start_date.value=energyData.today;f.elements.start_weight.value=weights.at(-1)?.weight??'';updatePlanPreview();f.elements.reason.focus();});
$('plan-info').addEventListener('click',()=>{$('plan-explanation').hidden=!$('plan-explanation').hidden;});
$('plan-ai').addEventListener('click',guarded(async()=>{
 const user_id=$('current-user').value;if(!user_id)throw Error('利用者を選択してください');if(!energyData?.active_plan)throw Error('先に減量計画を保存してください');
 $('plan-ai-answer').hidden=false;$('plan-ai-answer').textContent='AIで計画を分析中…';
 const question='現在の減量計画について、目標の計画線と代謝変化を含む試算の差、plan_analysis、直近の週次実績を使って説明してください。目標達成に向け、目標日の延長、標準摂取、生活活動、標準運動の4つをどう調整できるか具体的な候補を示し、無理な減量や安全ガード下限を下回る摂取は提案しないでください。推定と実測を区別し、優先順位ではなく選択肢とトレードオフとして示してください。';
 const result=await api('/api/ai/consult',{user_id,mode:'review',question});renderMarkdown($('plan-ai-answer'),result.answer);message('AIの計画分析を受け取りました');
}));
$('meal-estimate-form').addEventListener('submit',guarded(async()=>{
 const f=$('meal-estimate-form');const meals={date:f.elements.date.value,breakfast:f.elements.breakfast.value,lunch:f.elements.lunch.value,dinner:f.elements.dinner.value,snacks:f.elements.snacks.value};
 $('meal-estimate-result').hidden=false;$('meal-estimate-summary').textContent='AIで試算中…';
 try{const result=await api('/api/ai/meal-estimate',{meals});renderMarkdown($('meal-estimate-summary'),result.answer);}
 catch(error){$('meal-estimate-summary').textContent='試算できませんでした: '+error.message;throw error;}
}));
