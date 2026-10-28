"""Monte Carlo check of Home indicator reliability (see docs/health-metrics.md). Run: python3 scripts/health_metrics_simulation.py"""
import random, statistics as st
random.seed(1)
N = 4000
def ols(xs, ys):
    mx, my = st.mean(xs), st.mean(ys); sxx = sum((x-mx)**2 for x in xs)
    b = sum((x-mx)*(y-my) for x, y in zip(xs, ys))/sxx
    res = [y-(my+b*(x-mx)) for x, y in zip(xs, ys)]
    se = (sum(r*r for r in res)/(len(xs)-2)/sxx)**.5
    return b, se
def rmse(e): return (sum(x*x for x in e)/len(e))**.5
# 1) Weight pace: true -0.5 kg/week, day-to-day noise sd 0.6 kg (water/food), 1 of 4 days missing.
true = -0.5/7; sd = 0.6
res = {'前日比×7': [], '7日前の1点との差': [], '週平均どうしの差': [], '14日回帰': [], '28日回帰': []}
cover14 = cover28 = 0; sign28 = 0
for _ in range(N):
    days = [d for d in range(28) if random.random() > .25]
    w = {d: 75 + true*d + random.gauss(0, sd) for d in days}
    if 27 in w and 26 in w: res['前日比×7'].append((w[27]-w[26])*7 - true*7)
    if 27 in w and 20 in w: res['7日前の1点との差'].append(w[27]-w[20] - true*7)
    a = [w[d] for d in days if d >= 21]; b = [w[d] for d in days if 14 <= d < 21]
    if a and b: res['週平均どうしの差'].append(st.mean(a)-st.mean(b) - true*7)
    d14 = [d for d in days if d >= 14]
    if len(d14) >= 6:
        s, se = ols(d14, [w[d] for d in d14]); res['14日回帰'].append((s-true)*7); cover14 += abs(s-true) <= 2*se
    s, se = ols(days, [w[d] for d in days]); res['28日回帰'].append((s-true)*7); cover28 += abs(s-true) <= 2*se; sign28 += s+2*se < 0
print('## 減量ペース推定誤差 (真値 -0.50 kg/週, 日内変動SD 0.6kg, 25%欠測)')
for k, e in res.items(): print(f'{k}: RMSE {rmse(e):.2f} kg/週')
print(f'28日回帰の±2SE区間の被覆率 {cover28/N:.0%}, 減少を検出できた割合 {sign28/N:.0%}')
# 2) Fat share of loss: true 75% fat; BIA body-fat% noise sd 0.8 pt per reading.
for weeks, loss in [(4, 2.0), (8, 4.0), (12, 6.0)]:
    est = []
    for _ in range(N):
        n = weeks*7; days = [d for d in range(n) if random.random() > .25]
        W = [80 - loss*d/(n-1) + random.gauss(0, .6) for d in days]
        true_fat = [80*0.28 - 0.75*loss*d/(n-1) for d in days]
        bf = [(f/(80 - loss*d/(n-1)))*100 + random.gauss(0, .8) for f, d in zip(true_fat, days)]
        fm = [w*b/100 for w, b in zip(W, bf)]
        sw, _ = ols(days, W); sf, _ = ols(days, fm)
        est.append(sf/sw)
    est.sort(); lo, hi = est[int(N*.1)], est[int(N*.9)]
    print(f'脂肪比率 {weeks}週・{loss}kg減: 80%区間 {lo:.0%}〜{hi:.0%}')
# 3) Home BP mean: within-person reading sd ~8 mmHg systolic; classification stability at true 128.
for days in (3, 5, 7):
    ok = sum(125 <= st.mean([128+random.gauss(0, 8) for _ in range(days)]) < 135 for _ in range(N))/N
    print(f'家庭血圧 真値128: {days}日平均で「高値血圧」と正しく分類 {ok:.0%}')
# 4) Sleep duration: night-to-night sd 50 min; 7-night mean error.
e = [st.mean([random.gauss(0, 50) for _ in range(7)]) for _ in range(N)]
print(f'睡眠7夜平均の誤差 RMSE {rmse(e):.0f}分（夜ごとのばらつきSD50分）')
# 5) Steps 7-day average: day sd 3000.
e = [st.mean([random.gauss(0, 3000) for _ in range(7)]) for _ in range(N)]
print(f'歩数7日平均の誤差 RMSE {rmse(e):.0f}歩（日ごとのばらつきSD3000歩）')
