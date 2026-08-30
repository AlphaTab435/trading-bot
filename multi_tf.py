"""
Multi-timeframe strategy test + minimum profit filter
Tests: 1h, 2h, 4h, 8h, 12h, 1d
Also tests minimum profit threshold before allowing signal exit
"""
import requests, time, os
import pandas as pd
import numpy as np
from datetime import datetime

# ─────────────────────────────────────────
# DATA
# ─────────────────────────────────────────
def fetch_binance(symbol, interval, start='2021-01-01', limit_per_call=1000):
    url = 'https://api.binance.com/api/v3/klines'
    start_ts = int(datetime.strptime(start, '%Y-%m-%d').timestamp() * 1000)
    end_ts   = int(datetime.utcnow().timestamp() * 1000)
    rows, cur = [], start_ts
    calls = 0
    while cur < end_ts:
        try:
            resp = requests.get(url, params={'symbol': symbol, 'interval': interval,
                                'startTime': cur, 'endTime': end_ts,
                                'limit': limit_per_call}, timeout=10).json()
            if not resp: break
            rows.extend(resp)
            cur = resp[-1][0] + 1
            calls += 1
            if calls % 5 == 0:
                print(f'  {interval}: {len(rows)} candles fetched...')
            time.sleep(0.3)
        except Exception as e:
            print(f'  Fetch error: {e}')
            break
    cols = ['ts','open','high','low','close','volume',
            'ct','qv','tr','tbb','tbq','ign']
    df = pd.DataFrame(rows, columns=cols)
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c])
    return df.set_index('ts')[['open','high','low','close','volume']]

def get_data(interval, base_df_4h=None):
    """Get or derive data for a timeframe."""
    csv = f'btcusdt_{interval}.csv'

    # try to derive from 4h data (only for longer timeframes)
    if base_df_4h is not None and interval in ['8h','12h','1d']:
        rule = {'8h':'8h','12h':'12h','1d':'D'}[interval]
        df = base_df_4h.resample(rule).agg({
            'open': 'first', 'high': 'max',
            'low': 'min', 'close': 'last',
            'volume': 'sum'
        }).dropna()
        print(f'  {interval}: derived from 4h data ({len(df)} candles)')
        return df

    # load cached
    if os.path.exists(csv):
        df = pd.read_csv(csv, index_col=0, parse_dates=True)
        print(f'  {interval}: loaded {csv} ({len(df)} candles)')
        return df

    # download from Binance
    print(f'  {interval}: downloading from Binance...')
    df = fetch_binance('BTCUSDT', interval)
    df.to_csv(csv)
    print(f'  {interval}: saved {len(df)} candles to {csv}')
    return df


# ─────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────
def ema(s, n):  return s.ewm(span=n, adjust=False).mean()
def macd(s):
    m = ema(s,12)-ema(s,26); return m, ema(m,9)
def calc_adx(df, p=14):
    hi,lo,cl = df['high'],df['low'],df['close']
    tr  = pd.concat([hi-lo,(hi-cl.shift(1)).abs(),
                     (lo-cl.shift(1)).abs()],axis=1).max(axis=1)
    atr = tr.rolling(p).mean()
    up=hi-hi.shift(1); dn=lo.shift(1)-lo
    pdm=pd.Series(np.where((up>dn)&(up>0),up,0),index=df.index)
    ndm=pd.Series(np.where((dn>up)&(dn>0),dn,0),index=df.index)
    pdi=100*pdm.rolling(p).mean()/atr
    ndi=100*ndm.rolling(p).mean()/atr
    dx=100*(pdi-ndi).abs()/(pdi+ndi)
    return dx.rolling(p).mean(),pdi,ndi


# ─────────────────────────────────────────
# STRATEGY SIGNALS
# ─────────────────────────────────────────
def build_signals(df):
    """Same strategy logic applied to any timeframe."""
    ef=ema(df['close'],9); es=ema(df['close'],21); et=ema(df['close'],200)
    m,ms=macd(df['close'])
    adx4,pdi,ndi=calc_adx(df)
    vol_ma=df['volume'].rolling(20).mean()
    vol_r=df['volume']/vol_ma
    slope=(et-et.shift(10))/et.shift(10)*100

    # daily for MTF filter
    daily=df['close'].resample('D').last().dropna().to_frame()
    daily['high']=df['high'].resample('D').max()
    daily['low']=df['low'].resample('D').min()
    daily['volume']=df['volume'].resample('D').sum()
    e200d=ema(daily['close'],200)
    adxd,_,_=calc_adx(daily)
    distd=(daily['close']-e200d)/e200d*100
    e200d_h=e200d.reindex(df.index,method='ffill')
    adxd_h=adxd.reindex(df.index,method='ffill')
    distd_h=distd.reindex(df.index,method='ffill')

    buy=((ef>es)&(ef.shift(1)<=es.shift(1))
         &(m>ms)&(df['close']>et)
         &(adx4>20)&(pdi>ndi)
         &(vol_r>0.8)&(slope<0.5)
         &(df['close']>e200d_h)&(adxd_h>20)
         &(distd_h<30))
    sell=(ef<es)&(ef.shift(1)>=es.shift(1))
    return buy.astype(int), sell.astype(int)


# ─────────────────────────────────────────
# BACKTEST ENGINE
# Includes minimum profit filter:
# Signal exits only if trade covers fees + min_profit_pct
# Stop loss always exits regardless
# ─────────────────────────────────────────
def backtest(df, interval_label, capital=10.0, fee=0.001,
             stop_pct=0.08, min_profit_pct=0.0):
    FLOOR = 5.0
    ROUND_TRIP = 2 * fee
    buy_sig, sell_sig = build_signals(df)
    cash, pos, trades, equity = capital, None, [], []
    skipped_sells = 0   # times we held through a signal because not yet profitable

    for i in range(len(df)):
        row = df.iloc[i]
        s_buy  = buy_sig.iloc[i]
        s_sell = sell_sig.iloc[i]

        # stop loss — always exits regardless of profit
        if pos and row['close'] <= pos['stop']:
            pr = pos['qty'] * row['close'] * (1-fee)
            trades.append({'t':'sl','pnl':pr-pos['cost'],
                           'entry':pos['entry'],'exit':row['close']})
            cash += pr; pos = None

        total = cash + (pos['qty']*row['close'] if pos else 0)

        # buy
        if s_buy and pos is None and total >= FLOOR:
            qty = cash*(1-fee)/row['close']
            pos = {'entry':row['close'],'qty':qty,'cost':cash,
                   'stop':row['close']*(1-stop_pct)}
            cash = 0.0

        # sell — only if covers fees + minimum profit
        elif s_sell and pos:
            min_exit = pos['entry'] * (1 + ROUND_TRIP + min_profit_pct)
            if row['close'] >= min_exit:
                pr = pos['qty']*row['close']*(1-fee)
                trades.append({'t':'sig','pnl':pr-pos['cost'],
                               'entry':pos['entry'],'exit':row['close']})
                cash += pr; pos = None
            else:
                skipped_sells += 1  # hold — not profitable enough yet

        equity.append(cash + (pos['qty']*row['close'] if pos else 0))

    if pos:
        lp = df.iloc[-1]['close']
        pr = pos['qty']*lp*(1-fee)
        trades.append({'t':'end','pnl':pr-pos['cost'],
                       'entry':pos['entry'],'exit':lp})
        cash += pr

    wins=[t for t in trades if t['pnl']>0]
    loss=[t for t in trades if t['pnl']<=0]
    eq=pd.Series(equity,index=df.index); dd=(eq-eq.cummax())/eq.cummax()*100
    tw=sum(t['pnl'] for t in wins); tl=abs(sum(t['pnl'] for t in loss))
    final=equity[-1]

    return {
        'interval':      interval_label,
        'final':         final,
        'ret':           (final-capital)/capital*100,
        'tr':            len(trades),
        'wr':            len(wins)/len(trades)*100 if trades else 0,
        'dd':            dd.min(),
        'aw':            np.mean([t['pnl'] for t in wins]) if wins else 0,
        'al':            np.mean([t['pnl'] for t in loss]) if loss else 0,
        'pf':            tw/tl if tl>0 else 99,
        'sl':            len([t for t in trades if t['t']=='sl']),
        'skipped_sells': skipped_sells,
        'candles':       len(df),
        'min_equity':    eq.min(),
    }


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
if __name__ == '__main__':
    print('Loading data...')
    if os.path.exists('btcusdt_4h.csv'):
        df_4h = pd.read_csv('btcusdt_4h.csv', index_col=0, parse_dates=True)
        print(f'  4h: loaded {len(df_4h)} candles')
    else:
        print('  4h: btcusdt_4h.csv not found, downloading from Binance...')
        df_4h = fetch_binance('BTCUSDT', '4h', start='2021-01-01')
        df_4h.to_csv('btcusdt_4h.csv')
        print(f'  4h: downloaded and saved {len(df_4h)} candles')

    # ── PART 1: TIMEFRAME COMPARISON ─────────────────────────────
    print()
    print('Downloading additional timeframe data from Binance...')

    timeframes = {}
    # derive longer timeframes from existing 4h data
    for tf in ['8h','12h','1d']:
        timeframes[tf] = get_data(tf, base_df_4h=df_4h)
    timeframes['4h'] = df_4h

    # download shorter timeframes (requires Binance API access)
    for tf in ['1h','2h']:
        try:
            timeframes[tf] = get_data(tf)
        except Exception as e:
            print(f'  {tf}: skipped ({e})')

    print()
    print('Running backtest on all available timeframes...')
    print('(Same strategy, same parameters, only timeframe changes)')

    tf_results = []
    for tf_label in ['1h','2h','4h','8h','12h','1d']:
        if tf_label not in timeframes:
            print(f'  {tf_label}: no data, skipping')
            continue
        df = timeframes[tf_label]
        if len(df) < 300:
            print(f'  {tf_label}: not enough data ({len(df)} candles), skipping')
            continue
        r = backtest(df, tf_label, stop_pct=0.08)
        tf_results.append(r)
        print(f'  {tf_label}: done ({len(df)} candles, {r["tr"]} trades)')

    print()
    print('='*76)
    print('  TIMEFRAME COMPARISON — same strategy, same stop (8%)')
    print('='*76)
    print(f'  {"TF":<6} {"CANDLES":>8} {"RETURN":>8} {"TRADES":>7} '
          f'{"WIN%":>6} {"MAX DD":>8} {"PF":>5} {"SL":>4}')
    print('  '+'-'*68)
    for r in tf_results:
        flag=''
        if r['pf']>=2.0 and r['dd']>=-20 and r['ret']>100: flag=' <<'
        elif r['pf']>=1.5 and r['dd']>=-30 and r['ret']>50: flag=' <'
        print(f'  {r["interval"]:<6} {r["candles"]:>8} {r["ret"]:>7.1f}%'
              f' {r["tr"]:>7} {r["wr"]:>5.1f}% {r["dd"]:>7.1f}%'
              f' {r["pf"]:>5.2f} {r["sl"]:>4}{flag}')

    # ── PART 2: MINIMUM PROFIT FILTER TEST ───────────────────────
    print()
    print('='*76)
    print('  MINIMUM PROFIT FILTER — tested on 4h (our proven timeframe)')
    print('  Logic: signal exits ONLY if trade covers fees + min profit')
    print('  Stop loss always exits regardless of profit')
    print('='*76)
    print(f'  {"MIN PROFIT":>12} {"RETURN":>8} {"TRADES":>7} {"WIN%":>6} '
          f'{"MAX DD":>8} {"PF":>5} {"SKIPPED":>8}')
    print('  '+'-'*68)

    min_profits = [0.0, 0.003, 0.005, 0.008, 0.01, 0.015, 0.02]
    mp_results = []
    for mp in min_profits:
        r = backtest(df_4h, '4h', stop_pct=0.08, min_profit_pct=mp)
        r['min_profit'] = mp
        mp_results.append(r)
        label = f'{mp*100:.1f}%' if mp > 0 else '0% (baseline)'
        flag=''
        if r['pf']>=2.0 and r['dd']>=-20 and r['ret']>100: flag=' <<'
        elif r['pf']>=1.5 and r['dd']>=-30 and r['ret']>50: flag=' <'
        print(f'  {label:>12} {r["ret"]:>7.1f}% {r["tr"]:>7} '
              f'{r["wr"]:>5.1f}% {r["dd"]:>7.1f}% {r["pf"]:>5.2f}'
              f' {r["skipped_sells"]:>8}{flag}')

    print()
    print('  SKIPPED = times bot held through sell signal because not yet profitable enough')
    print()

    # best combination
    all_results = tf_results + mp_results
    best = sorted([r for r in all_results if r.get('pf',0)>=1.5 and r.get('ret',0)>80],
                  key=lambda x: x['pf']*(-x['dd']), reverse=True)
    if best:
        b = best[0]
        print('='*76)
        print(f'  BEST RESULT:')
        print(f'    Timeframe     : {b["interval"]}')
        if 'min_profit' in b:
            print(f'    Min profit    : {b["min_profit"]*100:.1f}%')
        print(f'    Return        : {b["ret"]:.2f}%  (${b["final"]:.4f})')
        print(f'    Trades        : {b["tr"]}')
        print(f'    Win rate      : {b["wr"]:.1f}%')
        print(f'    Max drawdown  : {b["dd"]:.2f}%')
        print(f'    Profit factor : {b["pf"]:.2f}')
        print(f'    Min equity    : ${b["min_equity"]:.4f}')
        print(f'    Stop losses   : {b["sl"]}')
