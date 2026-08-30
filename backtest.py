import pandas as pd
import numpy as np
import requests, time
from datetime import datetime

# ============================================================
# DATA
# ============================================================
def get_data(symbol='BTCUSDT', interval='4h', start='2021-01-01', end=None):
    if end is None:
        end = datetime.utcnow().strftime('%Y-%m-%d')
    url = 'https://api.binance.com/api/v3/klines'
    s = int(datetime.strptime(start, '%Y-%m-%d').timestamp() * 1000)
    e = int(datetime.strptime(end,   '%Y-%m-%d').timestamp() * 1000)
    rows, cur = [], s
    while cur < e:
        data = requests.get(url, params={'symbol':symbol,'interval':interval,
                            'startTime':cur,'endTime':e,'limit':1000}, timeout=10).json()
        if not data: break
        rows.extend(data); cur = data[-1][0] + 1
        print(f'  {len(rows)} candles...'); time.sleep(0.3)
    cols = ['ts','open','high','low','close','vol',
            'ct','qv','tr','tbb','tbq','ign']
    df = pd.DataFrame(rows, columns=cols)
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    for c in ['open','high','low','close','vol']:
        df[c] = pd.to_numeric(df[c])
    return df.set_index('ts')[['open','high','low','close','vol']]

# ============================================================
# INDICATORS
# ============================================================
def ema(s, n):    return s.ewm(span=n, adjust=False).mean()
def macd(s):
    m = ema(s,12)-ema(s,26); return m, ema(m,9)

# ============================================================
# STRATEGY  — final locked version
# Entry requires ALL 4 conditions:
#   1. EMA 9 crosses above EMA 21       (short-term momentum turns up)
#   2. MACD line above signal line      (momentum confirmed)
#   3. Price above 200 EMA              (long-term uptrend confirmed)
#   4. Capital >= $5                    (Binance minimum notional)
#
# Exit:
#   - EMA 9 crosses below EMA 21        (momentum turns down)
#   - OR price drops 5% below entry     (stop loss)
# ============================================================
def signals(df):
    e9, e21, e200 = ema(df['close'],9), ema(df['close'],21), ema(df['close'],200)
    m, sl = macd(df['close'])
    sig = pd.Series(0, index=df.index)
    sig[(e9>e21)&(e9.shift(1)<=e21.shift(1))&(m>sl)&(df['close']>e200)] = 1
    sig[(e9<e21)&(e9.shift(1)>=e21.shift(1))]                            = -1
    return sig

# ============================================================
# BACKTEST
# ============================================================
def backtest(df, sig, capital=10.0, fee=0.001, stop_pct=0.05):
    FLOOR   = 5.0       # Binance minimum notional
    cash    = capital
    pos     = None
    trades  = []
    equity  = []
    floor_breaches = 0  # candles where TOTAL value < $5

    for i in range(len(df)):
        row = df.iloc[i]
        s   = sig.iloc[i]

        # --- stop loss ---
        if pos and row['close'] <= pos['stop']:
            pr = pos['qty'] * row['close'] * (1-fee)
            trades.append({'type':'stop','date':row.name,
                           'entry':pos['entry'],'exit':row['close'],
                           'pnl':pr-pos['cost']})
            cash += pr
            pos   = None

        # --- total portfolio value ---
        total = cash + (pos['qty']*row['close'] if pos else 0)

        # --- floor breach counter (genuine inability to trade) ---
        if total < FLOOR:
            floor_breaches += 1

        # --- buy ---
        if s==1 and pos is None and total >= FLOOR:
            qty  = cash*(1-fee)/row['close']
            pos  = {'entry':row['close'],'qty':qty,'cost':cash,
                    'stop':row['close']*(1-stop_pct)}
            cash = 0.0

        # --- sell ---
        elif s==-1 and pos:
            pr = pos['qty']*row['close']*(1-fee)
            trades.append({'type':'signal','date':row.name,
                           'entry':pos['entry'],'exit':row['close'],
                           'pnl':pr-pos['cost']})
            cash += pr
            pos   = None

        equity.append(cash + (pos['qty']*row['close'] if pos else 0))

    # close any open position at end of data
    if pos:
        lp = df.iloc[-1]['close']
        pr = pos['qty']*lp*(1-fee)
        trades.append({'type':'end','date':df.index[-1],
                       'entry':pos['entry'],'exit':lp,
                       'pnl':pr-pos['cost']})
        cash += pr

    eq   = pd.Series(equity, index=df.index)
    dd   = (eq - eq.cummax()) / eq.cummax() * 100
    wins = [t for t in trades if t['pnl']>0]
    loss = [t for t in trades if t['pnl']<=0]
    tw   = sum(t['pnl'] for t in wins)
    tl   = abs(sum(t['pnl'] for t in loss))

    return {
        'final'   : equity[-1],
        'capital' : capital,
        'trades'  : trades,
        'equity'  : eq,
        'metrics' : {
            'return_pct'    : (equity[-1]-capital)/capital*100,
            'total_trades'  : len(trades),
            'wins'          : len(wins),
            'losses'        : len(loss),
            'win_rate'      : len(wins)/len(trades)*100 if trades else 0,
            'max_drawdown'  : dd.min(),
            'avg_win'       : np.mean([t['pnl'] for t in wins]) if wins else 0,
            'avg_loss'      : np.mean([t['pnl'] for t in loss]) if loss else 0,
            'profit_factor' : tw/tl if tl>0 else float('inf'),
            'stop_losses'   : len([t for t in trades if t['type']=='stop']),
            'floor_breaches': floor_breaches,
            'min_equity'    : eq.min(),
            'max_equity'    : eq.max(),
        }
    }

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    CSV = 'btcusdt_4h.csv'
    try:
        df   = pd.read_csv(CSV, index_col='timestamp', parse_dates=True)
        last = df.index[-1]
        if (pd.Timestamp.now() - last).days > 2:
            print(f'Updating data from {last.date()}...')
            new  = get_data('BTCUSDT','4h',(last+pd.Timedelta(hours=4)).strftime('%Y-%m-%d'))
            df   = pd.concat([df,new]).pipe(lambda x: x[~x.index.duplicated()])
            df.to_csv(CSV)
        print(f'Loaded {len(df)} candles  {df.index[0].date()} → {df.index[-1].date()}\n')
    except FileNotFoundError:
        print('Downloading data...')
        df = get_data(); df.to_csv(CSV)

    result = backtest(df, signals(df))
    m      = result['metrics']

    print('=' * 56)
    print('  FINAL STRATEGY RESULT')
    print('  EMA 9/21 cross + MACD confirm + 200 EMA filter + 5% stop')
    print(f'  {df.index[0].date()} → {df.index[-1].date()}')
    print('=' * 56)
    print(f'  Starting capital    : ${result["capital"]:.2f}')
    print(f'  Final capital       : ${result["final"]:.4f}')
    print(f'  Total return        : {m["return_pct"]:.2f}%')
    print()
    print(f'  Total trades        : {m["total_trades"]}')
    print(f'  Wins / Losses       : {m["wins"]} / {m["losses"]}')
    print(f'  Win rate            : {m["win_rate"]:.1f}%')
    print(f'  Avg win             : ${m["avg_win"]:.4f}')
    print(f'  Avg loss            : ${m["avg_loss"]:.4f}')
    print(f'  Win/loss ratio      : {abs(m["avg_win"]/m["avg_loss"]):.2f}x')
    print(f'  Profit factor       : {m["profit_factor"]:.2f}')
    print()
    print(f'  Max drawdown        : {m["max_drawdown"]:.2f}%')
    print(f'  Lowest portfolio    : ${m["min_equity"]:.4f}')
    print(f'  Highest portfolio   : ${m["max_equity"]:.4f}')
    print(f'  Stop losses hit     : {m["stop_losses"]}')
    print()
    floor_msg = "NEVER BREACHED" if m["floor_breaches"]==0 else f'BREACHED {m["floor_breaches"]} candles'
    print(f'  Binance floor ($5)  : {floor_msg}')
    print()

    stops = [t for t in result['trades'] if t['type']=='stop']
    if stops:
        print('  STOP LOSSES DETAIL:')
        for i,t in enumerate(stops):
            pnl_type = 'profit' if t['pnl']>0 else 'loss'
            print(f'  #{i+1}  {pd.Timestamp(t["date"]).date()}'
                  f'  entry=${t["entry"]:,.0f}'
                  f'  exit=${t["exit"]:,.0f}'
                  f'  {pnl_type} ${t["pnl"]:.4f}')

    print()
    print('=' * 56)
    print('  VERDICT')
    pf = m['profit_factor']
    dd = m['max_drawdown']
    fl = m['floor_breaches']
    if pf >= 1.5 and dd >= -40 and fl == 0:
        print('  PASS — strategy is ready for paper trading')
    else:
        issues = []
        if pf < 1.5:  issues.append(f'PF {pf:.2f} below 1.5')
        if dd < -40:  issues.append(f'drawdown {dd:.1f}% too deep')
        if fl > 0:    issues.append(f'hit Binance floor {fl} times')
        print(f'  ISSUES: {", ".join(issues)}')
    print('=' * 56)