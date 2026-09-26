'use strict';
const WELLNESS_FIELDS=['height_cm','age','sex','goal_weight_kg','goal_date','bp_sys_low','bp_sys_high','bp_dia_low','bp_dia_high','bmi_low','bmi_high','plan_text'];
async function refreshWellness(){
 const userId=$('current-user').value;
 $('wellness-empty').hidden=!!userId;$('wellness-content').hidden=!userId;
 if(!userId){wellnessProfile=null;wellnessProfileUser=null;renderBmi();renderCharts();return;}
 const [summary,meals,energy]=await Promise.all([api('/api/wellness/summary?user_id='+encodeURIComponent(userId)),api('/api/wellness/meals?user_id='+encodeURIComponent(userId)),api('/api/wellness/energy?user_id='+encodeURIComponent(userId))]);
 if(userId!==$('current-user').value)return;
 wellnessProfile=summary.profile;wellnessProfileUser=userId;renderBmi();const form=$('wellness-form');
 for(const key of WELLNESS_FIELDS)form.elements[key].value=summary.profile[key]??'';
 renderCharts();renderEnergy(energy,summary);
 const rows={'現在の体重':summary.latest_weight==null?'—':summary.latest_weight+' kg',
  'BMI（身長から算出）':summary.bmi??'身長・体重を設定してください',
  '推定基礎代謝':summary.estimated_bmr_kcal==null?'身長・年齢・計算条件・体重を設定してください':summary.estimated_bmr_kcal+' kcal/日',
  '直近の血圧':summary.latest_pressure?`${summary.latest_pressure.systolic} / ${summary.latest_pressure.diastolic} mmHg`:'—',
  '7日以上の体重変化':summary.weight_change_7d_kg==null?'比較できる記録がありません':`${summary.weight_change_7d_kg>0?'+':''}${summary.weight_change_7d_kg} kg`,
  '目標':summary.goal_weight_kg==null?'未設定':`${summary.goal_weight_kg} kg${summary.profile.goal_date?'（'+summary.profile.goal_date+'まで）':''}`,
  '目標への週あたりの変化':summary.weekly_change_needed_kg==null?'—':`${summary.weekly_change_needed_kg} kg/週の減量`};
 $('wellness-summary').replaceChildren();for(const [label,value] of Object.entries(rows))$('wellness-summary').append(element('dt',label),element('dd',String(value)));
 $('wellness-notices').replaceChildren();for(const notice of summary.notices)$('wellness-notices').append(element('p',notice,'notice'));if(!summary.notices.length)$('wellness-notices').append(element('p','現在の設定で追加の見直し通知はありません。','notice'));
 $('meal-list').replaceChildren();for(const m of meals.slice(0,20)){const row=element('div',undefined,'meal-row');row.append(element('span',dateText(m.created_at)+'　'+m.note+(m.calories===null?'':'　約'+m.calories+' kcal')));const remove=element('button','削除','danger');remove.type='button';remove.addEventListener('click',guarded(async()=>{if(!confirm('この食事メモを削除しますか？'))return;await api('/api/wellness/meals',{user_id:userId,id:m.id},'DELETE');await refreshWellness();message('食事メモを削除しました');}));const reuse=element('button','同じ内容を入力','secondary');reuse.type='button';reuse.onclick=()=>{const f=$('meal-form');f.elements.note.value=m.note;f.elements.calories.value=m.calories??'';f.elements.note.focus();};row.append(reuse,remove);$('meal-list').append(row);}
}
$('wellness-form').addEventListener('submit',guarded(async()=>{
 const user_id=$('current-user').value;if(!user_id)throw Error('利用者を選択してください');const f=$('wellness-form'),profile={};
 for(const key of WELLNESS_FIELDS){const v=f.elements[key].value;profile[key]=['height_cm','age','goal_weight_kg'].includes(key)?(v?Number(v):null):key.startsWith('bp_')||key.startsWith('bmi_')?Number(v):v;}
 await api('/api/wellness/profile',{user_id,profile});await refreshWellness();message('健康設定と計画を保存しました');
}));
$('meal-form').addEventListener('submit',guarded(async()=>{
 const user_id=$('current-user').value;if(!user_id)throw Error('利用者を選択してください');const f=$('meal-form');await api('/api/wellness/meals',{user_id,meal:{note:f.elements.note.value,calories:f.elements.calories.value?Number(f.elements.calories.value):null}});f.reset();await refreshWellness();message('食事メモを保存しました');
}));
$('coach-form').addEventListener('submit',guarded(async()=>{
 const user_id=$('current-user').value;if(!user_id)throw Error('利用者を選択してください');const f=$('coach-form');const result=await api('/api/ai/consult',{user_id,mode:f.elements.mode.value,question:f.elements.question.value});
 $('coach-answer').textContent=result.answer;$('coach-answer').hidden=false;$('coach-plan').hidden=false;message('相談への回答を受け取りました');
}));
$('coach-plan').addEventListener('click',()=>{const field=$('wellness-form').elements.plan_text;field.value=$('coach-answer').textContent.slice(0,8000);field.focus();message('計画欄へコピーしました。内容を確認して保存してください');});
async function refreshAiSettings(){const config=await api('/api/ai/settings');const f=$('ai-form');for(const key of ['provider','base_url','model'])f.elements[key].value=config[key];$('ai-status').textContent=config.key_configured?'APIキーを保存済み':'この接続先のAPIキーは未設定です';$('coach-provider').textContent='相談先: '+config.provider+(config.provider==='openrouter'?'（外部サービス）':'（ローカル）');}
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
 const actions=element('div',undefined,'row');const edit=element('button','編集','secondary');edit.type='button';edit.onclick=()=>{fillActivity(entry);$('activity-form').elements.reuse.value='once';$('activity-form').closest('details').open=true;$('activity-form').scrollIntoView({block:'center'});};const remove=element('button','削除','danger');remove.type='button';const userId=$('current-user').value;remove.onclick=guarded(async()=>{if(!confirm(entry.date+'の活動記録を削除しますか？'))return;await api('/api/wellness/activity',{user_id:userId,date:entry.date},'DELETE');await refreshWellness();message('その日の入力を削除しました。いつもの設定があればそれを表示します');});actions.append(edit);if(entry.source!=='routine')actions.append(remove);row.append(actions);list.append(row);}
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
 const result=await api('/api/ai/consult',{user_id,mode:'review',question});$('plan-ai-answer').textContent=result.answer;message('AIの計画分析を受け取りました');
}));
$('meal-estimate-form').addEventListener('submit',guarded(async()=>{
 const f=$('meal-estimate-form');const meals={date:f.elements.date.value,breakfast:f.elements.breakfast.value,lunch:f.elements.lunch.value,dinner:f.elements.dinner.value,snacks:f.elements.snacks.value};
 $('meal-estimate-result').hidden=false;$('meal-estimate-summary').textContent='AIで試算中…';
 try{const result=await api('/api/ai/meal-estimate',{meals});$('meal-estimate-summary').textContent=result.answer;}
 catch(error){$('meal-estimate-summary').textContent='試算できませんでした: '+error.message;throw error;}
}));
