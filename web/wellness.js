'use strict';
const WELLNESS_FIELDS=['height_cm','age','sex','goal_weight_kg','goal_date','bp_sys_low','bp_sys_high','bp_dia_low','bp_dia_high','bmi_low','bmi_high','plan_text'];
async function refreshWellness(){
 const userId=$('current-user').value;
 $('wellness-empty').hidden=!!userId;$('wellness-content').hidden=!userId;
 if(!userId){wellnessProfile=null;wellnessProfileUser=null;renderBmi();renderCharts();return;}
 const [summary,meals]=await Promise.all([api('/api/wellness/summary?user_id='+encodeURIComponent(userId)),api('/api/wellness/meals?user_id='+encodeURIComponent(userId))]);
 if(userId!==$('current-user').value)return;
 wellnessProfile=summary.profile;wellnessProfileUser=userId;renderBmi();const form=$('wellness-form');
 for(const key of WELLNESS_FIELDS)form.elements[key].value=summary.profile[key]??'';
 renderCharts();
 const rows={'現在の体重':summary.latest_weight==null?'—':summary.latest_weight+' kg',
  'BMI（身長から算出）':summary.bmi??'身長・体重を設定してください',
  '推定基礎代謝':summary.estimated_bmr_kcal==null?'身長・年齢・計算条件・体重を設定してください':summary.estimated_bmr_kcal+' kcal/日',
  '直近の血圧':summary.latest_pressure?`${summary.latest_pressure.systolic} / ${summary.latest_pressure.diastolic} mmHg`:'—',
  '7日以上の体重変化':summary.weight_change_7d_kg==null?'比較できる記録がありません':`${summary.weight_change_7d_kg>0?'+':''}${summary.weight_change_7d_kg} kg`,
  '目標':summary.goal_weight_kg==null?'未設定':`${summary.goal_weight_kg} kg${summary.profile.goal_date?'（'+summary.profile.goal_date+'まで）':''}`,
  '目標への週あたりの変化':summary.weekly_change_needed_kg==null?'—':`${summary.weekly_change_needed_kg} kg/週の減量`};
 $('wellness-summary').replaceChildren();for(const [label,value] of Object.entries(rows))$('wellness-summary').append(element('dt',label),element('dd',String(value)));
 $('wellness-notices').replaceChildren();for(const notice of summary.notices)$('wellness-notices').append(element('p',notice,'notice'));if(!summary.notices.length)$('wellness-notices').append(element('p','現在の設定で追加の見直し通知はありません。','notice'));
 $('meal-list').replaceChildren();for(const m of meals.slice(0,20)){const row=element('div',undefined,'meal-row');row.append(element('span',dateText(m.created_at)+'　'+m.note+(m.calories===null?'':'　約'+m.calories+' kcal')));const remove=element('button','削除','danger');remove.type='button';remove.addEventListener('click',guarded(async()=>{if(!confirm('この食事メモを削除しますか？'))return;await api('/api/wellness/meals',{user_id:userId,id:m.id},'DELETE');await refreshWellness();message('食事メモを削除しました');}));row.append(remove);$('meal-list').append(row);}
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
