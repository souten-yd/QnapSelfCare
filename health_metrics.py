"""Evidence-based diet and health indicators from stored records (no diagnosis).

Every indicator returns a status (good / ok / warn / unknown), the value shown, the
reference it is judged against and a confidence level. Thresholds and the minimum data
each indicator needs are documented in docs/health-metrics.md.
"""
from datetime import datetime, timedelta, timezone
import json
import math
from statistics import mean, median

import energy

JST = energy.JST
KCAL_PER_KG = 7700


def local_day(value):
    return datetime.fromisoformat(value).astimezone(JST).date()


def ols(points):
    """Least-squares slope per day, its standard error and the fitted value at the last x."""
    n = len(points)
    if n < 3:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in points) / sxx
    residual = sum((y - (my + slope * (x - mx))) ** 2 for x, y in points)
    se = math.sqrt(residual / (n - 2) / sxx) if n > 2 else float('inf')
    return slope, se, my + slope * (max(xs) - mx)


# Plain-language explanation shown on the 健康サポート indicators tab.
EXPLAIN = {
    'weight_pace': {
        'what': '体重が1週間あたりどのくらいのペースで変化しているか。',
        'how': '直近4週間の毎日の平均体重に直線を当てはめ（回帰）、その傾きを週あたりと体重比（%）で表します。±2標準誤差の幅が0をまたぐときは「はっきりしない」と判定します。',
        'why': '体重は水分や食事で1日に0.5〜1kg動くため、前日比では判断できません。回帰を使うと誤差は約±0.1kg/週まで小さくなります（シミュレーションで確認）。速すぎる減量は筋肉量の減少やリバウンドにつながりやすく、週に体重の1%以内が一般的な上限です。',
        'caveat': '体重計に乗る時間帯をそろえると精度が上がります。'},
    'fat_share': {
        'what': '減った体重のうち、どれだけが脂肪だったか。',
        'how': '体重×体脂肪率で脂肪量を求め、直近8週間の体重と脂肪量の変化の傾きを比べます。',
        'why': '食事制限だけの減量では、筋肉など除脂肪量が10〜30%ほど減るとされます。脂肪の割合が低いときは、たんぱく質や筋力運動を見直すきっかけになります。',
        'caveat': '体組成計（生体インピーダンス）の推定値は誤差が大きいため、8週間で3kg以上減ったときだけ判定し、信頼度は「低」です。'},
    'plan_gap': {
        'what': '保存した減量計画の線と比べて、いまどれだけ進んでいるか。',
        'how': '開始日と目標日を直線で結んだ計画線上の今日の体重と、平滑化した現在の体重の差です。',
        'why': '週ごとのずれに早く気づくと、目標日や摂取量の見直しが小さな調整で済みます。',
        'caveat': '計画線は単純な直線です。実際の減量は後半ほどゆるやかになりがちです。'},
    'goal_forecast': {
        'what': 'いまのペースが続いた場合に目標体重に届くおおよその日付。',
        'how': '残りの体重を現在の減量ペースで割って求めます。',
        'why': '目標日に間に合うかの目安になります。',
        'caveat': '代謝の低下などで減り方は鈍くなるため、予測は楽観的になりやすく信頼度は「低」です。'},
    'energy_consistency': {
        'what': '記録した摂取・消費と、実際の体重の動きが合っているか。',
        'how': '記録上の平均収支（摂取−基礎代謝−活動エネルギー）と、体重の変化から逆算した収支（1kg≒7,700kcal）の差を比べます。',
        'why': '差が大きいときは、食事の記録漏れや基礎代謝の推定ずれが考えられます。記録の精度を上げるヒントになります。',
        'caveat': '7,700kcal/kgは概算で、減量初期は水分の変動が大きいため信頼度は「低」です。'},
    'home_bp': {
        'what': '家庭で測った血圧の1週間の平均と、その分類。',
        'how': '直近7日の測定日ごとの平均を、さらに平均します。日本高血圧学会2019の家庭血圧の基準（正常<115/75、正常高値115〜124、高値125〜134または75〜84、高血圧≥135/85）で分類します。',
        'why': '家庭血圧は診察室の血圧より将来の病気との関連が強いとされ、複数日の平均で判断することが推奨されています。シミュレーションでは、7日平均だと3日平均より分類の正確さが上がりました（67%→83%）。',
        'caveat': '朝の起床後1時間以内（排尿後・朝食前）など、同じ条件で測るのがおすすめです。'},
    'bmi': {
        'what': '身長に対する体重の割合。',
        'how': '体重(kg)÷身長(m)²。体組成計の値があればそれを使います。',
        'why': '日本肥満学会の基準で18.5未満は低体重、25以上は肥満です。',
        'caveat': '筋肉量が多い人では高めに出ます。'},
    'visceral_fat': {
        'what': 'おなかの内臓まわりの脂肪の目安。',
        'how': 'オムロン体組成計が表示する内臓脂肪レベルをそのまま使います（1〜9 標準、10〜14 やや高い、15以上 高い）。',
        'why': '内臓脂肪は血圧・血糖・脂質の異常と関係が深いとされます。',
        'caveat': '機器独自の推定値で、CTで測る内臓脂肪面積とは異なり、メーカーによって基準も違います。'},
    'resting_hr': {
        'what': '安静にしているときの心拍数の目安と、その変化。',
        'how': 'ウォッチの心拍記録のうち、1日の下位10%の値を安静時の代わりに使い、直近7日と前の3週間を比べます。',
        'why': '安静時心拍が10上がるごとに死亡リスクが約9%高まるという報告があります（CMAJ 2016）。普段より5以上上がったときは、睡眠不足・疲れ・体調不良のサインのことがあります。',
        'caveat': 'ウォッチの安静時心拍そのものではなく、記録から推定した目安です。'},
    'sleep_duration': {
        'what': '1晩あたりの睡眠時間（7夜の平均）。',
        'how': 'ウォッチの睡眠記録から、覚醒・ベッドにいただけの時間を除き、重なりを1回だけ数えます。前日18時〜当日18時を1晩とします。',
        'why': '厚生労働省の睡眠ガイド2023は、成人に6時間以上を目安としています。7〜9時間を推奨域としています。',
        'caveat': 'ウォッチの睡眠判定は推定です。'},
    'sleep_regularity': {
        'what': '寝る・起きる時刻が毎日そろっているか。',
        'how': '直近14夜の睡眠の中央時刻のばらつき（標準偏差）を分で表します。30分以内を良好とします。',
        'why': '睡眠の規則性は、睡眠時間よりも死亡リスクとの関連が強いという大規模研究があります（Sleep 2024、UK Biobank）。',
        'caveat': '研究で使われる指標（SRI）を単純化した代わりの指標です。'},
    'steps': {
        'what': '1日の歩数（今日を除く7日の平均）。',
        'how': 'ウォッチ・iPhoneの歩数記録を日ごとに合計します。記録元が複数あるときは件数が最も多い記録元だけを数えます。',
        'why': '厚生労働省の身体活動ガイド2023は、成人で1日約8,000歩、高齢者で約6,000歩を目安としています。死亡リスクの低下は60歳未満で8,000〜10,000歩、60歳以上で6,000〜8,000歩で頭打ちになるという報告があります（Lancet Public Health 2022）。',
        'caveat': '目安は年齢（身体情報）から自動で切り替えます。Homeの歩数目標とは別です。'},
}


def metric(key, label, value, status, detail, reference, confidence, short=None):
    return {'id': key, 'label': label, 'value': value, 'status': status, 'detail': detail,
            'reference': reference, 'confidence': confidence, 'short': short, 'explain': EXPLAIN.get(key)}


def unknown(key, label, detail, reference):
    return metric(key, label, '—', 'unknown', detail, reference, 'none')


def daily_means(records, field, start, end):
    grouped = {}
    for r in records:
        v = r['values'].get(field)
        if v is None:
            continue
        d = local_day(r['measured_at'])
        if start <= d <= end:
            grouped.setdefault(d, []).append(v)
    return {d: mean(v) for d, v in sorted(grouped.items())}


# ---- Diet evaluation ----

def weight_pace(records, today, goal):
    """28-day regression of daily mean weight: noise-robust weekly pace (see simulation)."""
    key, label, ref = 'weight_pace', '減量ペース', '週に体重の0.25〜1.0%（1か月あたり約1〜4%）'
    start = today - timedelta(days=27)
    days = daily_means(records, 'weight', start, today)
    if len(days) < 10 or (max(days) - min(days)).days < 21:
        return unknown(key, label, '直近4週間で10日以上（3週間以上の幅）の体重記録が必要です。', ref), None
    fit = ols([((d - start).days, w) for d, w in days.items()])
    slope, se, trend = fit
    week, pct = slope * 7, slope * 7 / trend * 100
    margin = 2 * se * 7
    losing = goal is not None and goal < trend
    value = f'{week:+.2f} kg/週（{pct:+.2f}%）'
    unclear = abs(week) < margin
    if unclear and not losing:
        status, detail = 'good', f'安定しています（はっきりした増減なし、±{margin:.2f} kg/週の幅）。'
    elif unclear:
        status, detail = 'ok', f'変化ははっきりしません（±{margin:.2f} kg/週の幅）。'
    elif losing and -1.0 <= pct <= -0.25:
        status, detail = 'good', '無理のない範囲で減っています。'
    elif losing and pct < -1.0:
        status, detail = 'warn', '速すぎます。筋肉量の減少や体調に注意し、計画を見直してください。'
    elif losing and pct < 0:
        status, detail = 'ok', 'ゆるやかに減っています。'
    elif losing:
        status, detail = 'warn', '目標とは逆に増えています。'
    else:
        status, detail = ('good', '安定しています。') if abs(pct) < 0.25 else ('ok', '体重が変化しています。')
    detail += f' 平滑化した現在の体重は{trend:.1f} kg（{len(days)}日分の回帰）。'
    word = {'good': '適正', 'ok': '要観察', 'warn': '要見直し'}[status]
    short = ('体重は安定しています' if not losing else '体重の変化ははっきりしません') if unclear else f'減量ペースは週{abs(week):.2f}kg{"減" if week < 0 else "増"}で{word}'
    return metric(key, label, value, status, detail, ref, 'high' if len(days) >= 18 else 'medium', short), \
        {'slope': slope, 'se': se, 'trend': trend, 'days': len(days)}


def fat_share(records, today):
    """Share of weight change that came from fat mass (BIA). Needs a long window."""
    key, label, ref = 'fat_share', '減量の中身（脂肪の割合）', '一般的な減量で脂肪70〜90%（除脂肪10〜30%）'
    start = today - timedelta(days=55)
    w = daily_means(records, 'weight', start, today)
    bf = daily_means(records, 'body_fat', start, today)
    both = [d for d in w if d in bf]
    if len(both) < 20:
        return unknown(key, label, '直近8週間で体脂肪率つきの記録が20日以上必要です。', ref)
    sw = ols([((d - start).days, w[d]) for d in both])
    sf = ols([((d - start).days, w[d] * bf[d] / 100) for d in both])
    span = (max(both) - min(both)).days
    change = sw[0] * span
    if round(change, 1) > -3:
        return unknown(key, label, f'8週間の体重変化（{change:+.1f} kg）が小さく、体組成計の誤差より大きな差が出ていません。3 kg以上の減量で判定します。', ref)
    share = sf[0] / sw[0]
    value = f'脂肪 {share:.0%} ／ 除脂肪 {1 - share:.0%}'
    if share >= 0.7:
        status, detail = 'good', '主に脂肪が減っています。'
    elif share >= 0.5:
        status, detail = 'ok', '筋肉など除脂肪量の減少がやや多めです。たんぱく質と筋力運動を確認してください。'
    else:
        status, detail = 'warn', '除脂肪量の減少が多めです。減量速度・たんぱく質・筋力運動を見直してください。'
    return metric(key, label, value, status, detail + ' 体組成計（生体インピーダンス）の推定値に基づきます。', ref, 'low',
                  f'減った体重のうち脂肪は約{share:.0%}')


def plan_gap(pace, plan, today):
    key, label, ref = 'plan_gap', '計画との差', '計画線との差 ±0.5 kg以内'
    if not plan:
        return unknown(key, label, '減量計画を保存すると判定します。', ref)
    if not pace:
        return unknown(key, label, '減量ペースを出せる記録がそろうと判定します。', ref)
    a, z = energy.day(plan['start_date']), energy.day(plan['goal_date'])
    total = max(1, (z - a).days)
    ratio = min(1, max(0, (today - a).days / total))
    planned = plan['start_weight'] + (plan['goal_weight'] - plan['start_weight']) * ratio
    gap = pace['trend'] - planned
    status = 'good' if gap <= 0.5 else 'ok' if gap <= 1.5 else 'warn'
    detail = '計画どおりです。' if gap <= 0.5 else '計画より遅れています。'
    return metric(key, label, f'{gap:+.1f} kg', status, f'{detail} 計画上の今日の体重は{planned:.1f} kg。', ref, 'medium')


def goal_forecast(pace, goal, goal_date, today):
    key, label, ref = 'goal_forecast', '目標到達の見込み', '現在のペースを延長した単純予測'
    if goal is None:
        return unknown(key, label, '目標体重を設定すると予測します。', ref)
    if not pace:
        return unknown(key, label, '減量ペースを出せる記録がそろうと予測します。', ref)
    remaining = pace['trend'] - goal
    if remaining <= 0:
        return metric(key, label, '到達済み', 'good', '平滑化した体重が目標に到達しています。', ref, 'medium')
    if pace['slope'] + 2 * pace['se'] >= 0:
        return metric(key, label, '予測できません', 'warn', '現在は減少傾向がはっきりしないため、到達日を予測できません。', ref, 'medium')
    days = remaining / -pace['slope']
    when = today + timedelta(days=round(days))
    if goal_date:
        late = (when - energy.day(goal_date)).days
        status = 'good' if late <= 0 else 'ok' if late <= 30 else 'warn'
        detail = '目標日に間に合う見込みです。' if late <= 0 else f'目標日より約{late}日遅れる見込みです。'
    else:
        status, detail = 'ok', '目標日を設定すると間に合うかを判定します。'
    return metric(key, label, f'{when.year}/{when.month}/{when.day}ごろ', status,
                  detail + ' 体重の減り方は途中で緩やかになるため、実際は遅くなりがちです。', ref, 'low')


def energy_consistency(history, pace, today):
    """Logged balance vs the balance implied by the weight trend (7,700 kcal per kg)."""
    key, label, ref = 'energy_consistency', '記録と体重変化の整合', '差が1日300 kcal以内'
    if not pace:
        return unknown(key, label, '減量ペースを出せる記録がそろうと判定します。', ref)
    start = today - timedelta(days=27)
    rows = [h for h in history if start.isoformat() <= h['date'] < today.isoformat() and h.get('balance') is not None]
    if len(rows) < 14:
        return unknown(key, label, '直近4週間で摂取とウォッチの活動エネルギーがそろった日が14日以上必要です。', ref)
    logged = mean(h['balance'] for h in rows)
    implied = pace['slope'] * KCAL_PER_KG
    diff = logged - implied
    status = 'good' if abs(diff) <= 300 else 'ok' if abs(diff) <= 600 else 'warn'
    hint = '記録と体重の動きがおおむね一致しています。' if abs(diff) <= 300 else \
        '体重の動きより摂取が少なく記録されています。記録漏れや基礎代謝の推定ずれが考えられます。' if diff < 0 else \
        '体重の動きより摂取が多く記録されています。活動エネルギーの取りこぼしや推定ずれが考えられます。'
    return metric(key, label, f'{diff:+.0f} kcal/日', status,
                  f'{hint} 記録上の平均収支 {logged:+.0f} kcal/日、体重変化からの推定 {implied:+.0f} kcal/日（{len(rows)}日）。', ref, 'low')


# ---- Body status ----

def home_bp(records, today, profile):
    key, label, ref = 'home_bp', '家庭血圧（7日平均）', '日本高血圧学会2019 家庭血圧: 正常<115/75、高血圧≥135/85'
    start = today - timedelta(days=6)
    sys_d = daily_means(records, 'systolic', start, today)
    dia_d = daily_means(records, 'diastolic', start, today)
    days = [d for d in sys_d if d in dia_d]
    if len(days) < 3:
        return unknown(key, label, '直近7日で3日以上の測定が必要です（5日以上を推奨）。', ref)
    s, d = mean(sys_d[x] for x in days), mean(dia_d[x] for x in days)
    if s >= 135 or d >= 85:
        cls, status = '高血圧の範囲', 'warn'
    elif s >= 125 or d >= 75:
        cls, status = '高値血圧', 'warn'
    elif s >= 115:
        cls, status = '正常高値血圧', 'ok'
    else:
        cls, status = '正常血圧', 'good'
    detail = f'{cls}（{len(days)}日分の平均）。'
    if status == 'warn' and cls == '高血圧の範囲':
        detail += ' 続く場合は医療機関に相談してください。'
    if len(days) < 5:
        detail += ' 測定日が少ないため、分類が変わる可能性があります。'
    return metric(key, label, f'{s:.0f}/{d:.0f} mmHg', status, detail, ref, 'high' if len(days) >= 5 else 'medium',
                  f'家庭血圧の7日平均は{s:.0f}/{d:.0f}で{cls}')


def resting_hr(samples, today):
    """Daily 10th percentile of heart-rate samples as a resting-rate proxy; 7 days vs prior 21."""
    key, label, ref = 'resting_hr', '安静時心拍の目安', '成人の安静時60〜100 bpm、80超は高め（10 bpm上昇ごとに死亡リスク約9%増）'
    by_day = {}
    for s in samples:
        if s['metric'] == 'heart_rate':
            by_day.setdefault(local_day(s['start_at']), []).append(s['value'])
    def low(values):
        values = sorted(values)
        return values[max(0, math.ceil(len(values) * 0.1) - 1)]
    daily = {d: low(v) for d, v in by_day.items() if len(v) >= 5}
    recent = [v for d, v in daily.items() if today - timedelta(days=6) <= d <= today]
    before = [v for d, v in daily.items() if today - timedelta(days=27) <= d < today - timedelta(days=6)]
    if len(recent) < 3:
        return unknown(key, label, '直近7日で心拍が1日5件以上記録された日が3日以上必要です。', ref)
    value = mean(recent)
    change = value - mean(before) if len(before) >= 5 else None
    if value > 100:
        status, detail = 'warn', '安静時の心拍が高めです。'
    elif value > 80 or (change is not None and change >= 5):
        status, detail = 'ok', '高め、または上昇傾向です。睡眠不足・体調・疲労を確認してください。'
    else:
        status, detail = 'good', '良好な範囲です。'
    text = f'{value:.0f} bpm' + (f'（前3週比 {change:+.0f}）' if change is not None else '')
    return metric(key, label, text, status, detail + ' ウォッチの心拍記録の下位10%から推定した目安です。', ref, 'medium',
                  f'安静時心拍の目安は{text}で{"高め" if value > 80 else "上昇傾向"}')


def sleep_nights(samples, today, nights):
    """Per-night asleep time (18:00-18:00 JST window) and midpoint, overlaps counted once."""
    asleep = [s for s in samples if s['metric'] == 'sleep' and s['value'] in ('core', 'deep', 'rem', 'asleep_unspecified')]
    out = {}
    for i in range(nights):
        d = today - timedelta(days=i)
        b = datetime(d.year, d.month, d.day, 18, tzinfo=JST)
        a = b - timedelta(days=1)
        spans = sorted((max(a, datetime.fromisoformat(s['start_at']).astimezone(JST)), min(b, datetime.fromisoformat(s['end_at']).astimezone(JST)))
                       for s in asleep if datetime.fromisoformat(s['end_at']) > a and datetime.fromisoformat(s['start_at']) < b)
        total, end, weighted = 0.0, None, 0.0
        for x, y in spans:
            x = max(x, end) if end else x
            if y > x:
                minutes = (y - x).total_seconds() / 60
                total += minutes
                weighted += minutes * ((x - a).total_seconds() / 60 + minutes / 2)
            end = max(end, y) if end else y
        if total > 0:
            out[d] = {'minutes': total, 'midpoint': weighted / total}
    return out


def sleep_duration(nights):
    key, label, ref = 'sleep_duration', '睡眠時間（7夜平均）', '睡眠ガイド2023: 成人6時間以上、7〜9時間が推奨域'
    recent = [n['minutes'] for d, n in nights.items() if d >= max(nights) - timedelta(days=6)] if nights else []
    if len(recent) < 3:
        return unknown(key, label, '直近7夜で3夜以上の睡眠記録が必要です。', ref)
    hours = mean(recent) / 60
    if 7 <= hours <= 9:
        status, detail = 'good', '推奨域です。'
    elif 6 <= hours < 7:
        status, detail = 'ok', '6時間以上ありますが、7時間未満です。'
    elif hours > 9:
        status, detail = 'ok', '長めです。日中の眠気や体調も確認してください。'
    else:
        status, detail = 'warn', '6時間未満です。睡眠時間の確保を優先してください。'
    text = f'{int(hours)}時間{round(hours % 1 * 60):02d}分'
    return metric(key, label, text, status, detail + f' {len(recent)}夜分。ウォッチの推定値です。', ref, 'medium',
                  f'睡眠の7夜平均は{text}と{"短め" if hours < 7 else "長め" if hours > 9 else "推奨域"}')


def sleep_regularity(nights):
    key, label, ref = 'sleep_regularity', '睡眠の規則性', '睡眠中央時刻のばらつき（標準偏差）30分以内が目安'
    if len(nights) < 5:
        return unknown(key, label, '直近14夜で5夜以上の睡眠記録が必要です。', ref)
    mids = [n['midpoint'] for n in nights.values()]
    sd = (sum((m - mean(mids)) ** 2 for m in mids) / (len(mids) - 1)) ** 0.5
    status = 'good' if sd <= 30 else 'ok' if sd <= 60 else 'warn'
    detail = {'good': '寝る・起きる時刻が安定しています。', 'ok': '時刻にややばらつきがあります。',
              'warn': '時刻のばらつきが大きいです。起床時刻をそろえることから始められます。'}[status]
    return metric(key, label, f'±{sd:.0f}分', status, detail + f' {len(mids)}夜分。規則的な睡眠は睡眠時間より死亡リスクとの関連が強いという報告があります。', ref, 'medium',
                  f'睡眠時刻のばらつきが±{sd:.0f}分と大きめ')


def daily_steps(samples, today, age):
    target = 6000 if age is not None and age >= 65 else 8000
    key, label = 'steps', '歩数（7日平均）'
    ref = f'身体活動ガイド2023: {"高齢者 約6,000" if target == 6000 else "成人 約8,000"}歩/日'
    by_day = {}
    counts = {}
    for s in samples:
        if s['metric'] == 'steps':
            counts[s['source']] = counts.get(s['source'], 0) + 1
    source = max(counts, key=counts.get) if counts else None
    for s in samples:
        if s['metric'] == 'steps' and s['source'] == source:
            d = local_day(s['start_at'])
            by_day[d] = by_day.get(d, 0) + s['value']
    recent = [v for d, v in by_day.items() if today - timedelta(days=7) <= d < today]
    if len(recent) < 4:
        return unknown(key, label, '今日を除く直近7日で4日以上の歩数記録が必要です。', ref)
    avg = mean(recent)
    status = 'good' if avg >= target else 'ok' if avg >= target * 0.75 else 'warn'
    detail = {'good': '目安を満たしています。', 'ok': '目安にもう少しです。', 'warn': '目安を下回っています。生活の中で歩く機会を増やせます。'}[status]
    return metric(key, label, f'{avg:,.0f} 歩', status, detail + f' {len(recent)}日分（今日を除く）。', ref, 'high',
                  f'歩数の7日平均は{avg:,.0f}歩で目安の{target:,}歩未満')


def visceral_fat(records):
    key, label, ref = 'visceral_fat', '内臓脂肪レベル', 'オムロン体組成計の判定: 1〜9 標準、10〜14 やや高い、15以上 高い'
    latest = next((r for r in sorted(records, key=lambda r: r['measured_at'], reverse=True) if r['values'].get('visceral_fat') is not None), None)
    if not latest:
        return unknown(key, label, '内臓脂肪レベルの記録がありません。', ref)
    v = latest['values']['visceral_fat']
    status, cls = ('good', '標準') if v < 10 else ('ok', 'やや高い') if v < 15 else ('warn', '高い')
    return metric(key, label, f'{v:g}', status, f'{cls}。機器の推定値で、CTの内臓脂肪面積とは異なります。', ref, 'low')


def bmi_status(records, profile):
    key, label, ref = 'bmi', 'BMI', '日本肥満学会: 18.5未満 低体重、25以上 肥満'
    latest = next((r for r in sorted(records, key=lambda r: r['measured_at'], reverse=True) if r['values'].get('weight') is not None), None)
    height = profile.get('height_cm')
    if not latest or not height:
        return unknown(key, label, '身長と体重の記録が必要です。', ref)
    bmi = latest['values'].get('bmi') or latest['values']['weight'] / (height / 100) ** 2
    if bmi < 18.5:
        status, cls = 'warn', '低体重'
    elif bmi < 25:
        status, cls = 'good', '普通体重'
    elif bmi < 30:
        status, cls = 'ok', '肥満（1度）'
    else:
        status, cls = 'warn', '肥満（2度以上）'
    return metric(key, label, f'{bmi:.1f}', status, f'{cls}。筋肉量が多い人では高めに出ます。', ref, 'high')


# ---- Summary ----

def summary_sentences(diet, health):
    """Short evaluative sentences for the Home header: the pace first, then warnings."""
    by = {m['id']: m for m in diet + health}
    out = []
    pace = by.get('weight_pace')
    if pace and pace['status'] != 'unknown':
        out.append(pace['short'] + '。')
    for key in ('home_bp', 'sleep_duration', 'resting_hr', 'fat_share', 'sleep_regularity', 'steps'):
        m = by.get(key)
        if m and m['status'] == 'warn' and m.get('short'):
            out.append(m['short'] + '。')
    return out[:3]


def compute(store, user_id, current=None):
    today = current or energy.today()
    profile = store.wellness_profile(user_id)
    since = datetime(today.year, today.month, today.day, tzinfo=JST) - timedelta(days=60)
    utc = lambda value: value.astimezone(timezone.utc).isoformat()  # stored timestamps are UTC ISO strings
    with store.connect() as db:
        records = [store.unpack(r) for r in db.execute(
            'SELECT * FROM measurements WHERE user_id=? AND measured_at>=? ORDER BY measured_at', (user_id, utc(since)))]
        samples = [json.loads(r[0]) for r in db.execute(
            'SELECT payload FROM apple_health_samples WHERE user_id=? AND end_at>=? ORDER BY start_at',
            (user_id, utc(since + timedelta(days=32))))]
    report = store.energy_report(user_id)
    plan = report.get('active_plan')
    goal = plan['goal_weight'] if plan else profile.get('goal_weight_kg')
    goal_date = plan['goal_date'] if plan else profile.get('goal_date') or None
    # Daily logged balance = intake - (BMR + recorded active energy), as on the Home screen.
    active = {}
    for s in samples:
        if s['metric'] == 'active_energy':
            d = local_day(s['start_at']).isoformat()
            active.setdefault(d, {}).setdefault(s['source'], 0)
            active[d][s['source']] += s['value']
    history = []
    for h in report.get('history', []):
        a = max(active.get(h['date'], {}).values(), default=None)
        balance = h['intake_kcal'] - (h['bmr'] + a) if h.get('intake_kcal') is not None and h.get('bmr') is not None and a is not None else None
        history.append({'date': h['date'], 'balance': balance})
    body = [r for r in records if r['kind'] == 'body_composition']
    pressure = [r for r in records if r['kind'] == 'blood_pressure']
    pace_metric, pace = weight_pace(body, today, goal)
    nights = sleep_nights(samples, today, 14)
    diet = [pace_metric, fat_share(body, today), plan_gap(pace, plan, today),
            goal_forecast(pace, goal, goal_date, today), energy_consistency(history, pace, today)]
    health = [home_bp(pressure, today, profile), bmi_status(body, profile), visceral_fat(body),
              resting_hr(samples, today), sleep_duration(nights), sleep_regularity(nights),
              daily_steps(samples, today, profile.get('age'))]
    return {'date': today.isoformat(), 'diet': diet, 'health': health, 'summary': summary_sentences(diet, health)}


# ---- Daily review: one day compared with the 7 days before it ----

def _compare(key, label, unit, value, history, higher_is_better=None, digits=0, band=0.1):
    """value vs mean of the previous days; band is the ±share treated as 'usual'."""
    prior = [v for v in history if v is not None]
    if value is None:
        return {'id': key, 'label': label, 'unit': unit, 'value': None, 'avg7': round(mean(prior), digits) if prior else None,
                'diff': None, 'trend': 'none', 'status': 'unknown'}
    avg = mean(prior) if len(prior) >= 3 else None
    diff = value - avg if avg is not None else None
    if diff is None:
        trend = 'none'
    elif abs(diff) <= abs(avg) * band:
        trend = 'usual'
    else:
        trend = 'up' if diff > 0 else 'down'
    status = 'ok' if trend in ('usual', 'none') or higher_is_better is None else \
        'good' if (trend == 'up') == higher_is_better else 'warn'
    r = (lambda v: round(v, digits) if v is not None else None)
    return {'id': key, 'label': label, 'unit': unit, 'value': r(value), 'avg7': r(avg), 'diff': r(diff), 'trend': trend, 'status': status}


def day_review(store, user_id, day):
    """Deterministic review of `day` (JST date) against the previous 7 days. Works without AI."""
    start = day - timedelta(days=7)
    utc = lambda value: value.astimezone(timezone.utc).isoformat()
    since = datetime(start.year, start.month, start.day, tzinfo=JST) - timedelta(hours=6)
    until = datetime(day.year, day.month, day.day, tzinfo=JST) + timedelta(days=1)
    with store.connect() as db:
        if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            raise ValueError('利用者が見つかりません')
        records = [store.unpack(r) for r in db.execute(
            'SELECT * FROM measurements WHERE user_id=? AND measured_at>=? AND measured_at<? ORDER BY measured_at',
            (user_id, utc(since), utc(until)))]
        samples = [json.loads(r[0]) for r in db.execute(
            'SELECT payload FROM apple_health_samples WHERE user_id=? AND end_at>=? AND start_at<? ORDER BY start_at',
            (user_id, utc(since), utc(until)))]
    days = [start + timedelta(days=i) for i in range(8)]  # 7 previous days + the day itself
    totals = {d: {} for d in days}
    for metric_key in ('steps', 'active_energy'):
        counts = {}
        for s in samples:
            if s['metric'] == metric_key:
                counts[s['source']] = counts.get(s['source'], 0) + 1
        source = max(counts, key=counts.get) if counts else None
        for s in samples:
            if s['metric'] == metric_key and s['source'] == source:
                d = local_day(s['start_at'])
                if d in totals:
                    totals[d][metric_key] = totals[d].get(metric_key, 0) + s['value']
    nights = sleep_nights(samples, day, 8)
    report = store.energy_report(user_id)
    hist = {h['date']: h for h in report.get('history', [])}
    entries = {e['date']: e for e in report.get('entries', report.get('diaries', []))}
    def series(fn):
        return [fn(d) for d in days[:-1]], fn(day)
    def balance(d):
        h, a = hist.get(d.isoformat(), {}), totals[d].get('active_energy')
        return h['intake_kcal'] - (h['bmr'] + a) if h.get('intake_kcal') is not None and h.get('bmr') is not None and a is not None else None
    weights = daily_means([r for r in records if r['kind'] == 'body_composition'], 'weight', start, day)
    systolic = daily_means([r for r in records if r['kind'] == 'blood_pressure'], 'systolic', start, day)
    items = []
    for key, label, unit, fn, better, digits in (
            ('steps', '歩数', '歩', lambda d: totals[d].get('steps'), True, 0),
            ('active_energy', '活動エネルギー', 'kcal', lambda d: totals[d].get('active_energy'), True, 0),
            ('exercise', '運動時間', '分', lambda d: (entries.get(d.isoformat()) or {}).get('minutes'), True, 0),
            ('sleep', '睡眠時間', '時間', lambda d: nights[d]['minutes'] / 60 if d in nights else None, None, 1),
            ('intake', '摂取', 'kcal', lambda d: hist.get(d.isoformat(), {}).get('intake_kcal'), False, 0),
            ('balance', '収支（摂取−基礎代謝−活動）', 'kcal', balance, False, 0),
            ('weight', '体重', 'kg', lambda d: weights.get(d), False, 1),
            ('systolic', '最高血圧', 'mmHg', lambda d: systolic.get(d), False, 0)):
        before, value = series(fn)
        band = 0.005 if key == 'weight' else 0.05 if key == 'systolic' else 0.1
        items.append(_compare(key, label, unit, value, before, better, digits, band))
    by = {i['id']: i for i in items}
    if by['sleep']['value'] is not None:
        hours = by['sleep']['value']
        by['sleep']['status'] = 'good' if 7 <= hours <= 9 else 'ok' if hours >= 6 else 'warn'
    if by['balance']['value'] is not None:
        by['balance']['status'] = 'good' if by['balance']['value'] <= 0 else 'warn'
    recorded = [i for i in items if i['value'] is not None]
    notes = []
    for i in items:
        if i['trend'] in ('up', 'down') and i['avg7'] is not None:
            word = 'いつもより多め' if i['trend'] == 'up' else 'いつもより少なめ'
            if i['id'] in ('weight', 'systolic'):
                word = 'いつもより高め' if i['trend'] == 'up' else 'いつもより低め'
            if i['id'] == 'balance':
                word = 'いつもより摂取が上回る' if i['trend'] == 'up' else 'いつもより消費が上回る'
            notes.append(f"{i['label']}は{word}（{i['value']:g}{i['unit']}、7日平均{i['avg7']:g}{i['unit']}）")
    return {'date': day.isoformat(), 'items': items, 'recorded': len(recorded), 'notes': notes[:4]}
