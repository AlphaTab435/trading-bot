"""
Multi-pair backtest: same strategy on BTC, ETH, SOL, BNB
Capital allocation: one position at a time, first signal wins
"""
import requests, time, os
import pandas as pd
import numpy as np
from datetime import datetime

PAIRS  = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT']
START  = '2021-01-01'
STOP   = 0.08
FEE    = 0.001

def fetch(symbol, interval='4h', start=START):
    csv = f'{symbol.lower()}_{interval}.csv'
    if os.path.exists(csv):
        df = pd.read_csv(csv, index_col=0, parse_dates=True)
        print(f'  {symbol}: loaded {len(df)} candles')
        return df
    print(f'  {symbol}: downloading...')
    url = 'https://api.binance.com/api/v3/klines'
    start_ts = int(datetime.strptime(start,'%Y-%m-%d').timestamp()*1000)
    end_ts   = int(datetime.now().timestamp()*1000)
    rows, cur = [], start_ts
    while cur < end_ts:
        r = requests.get(url, params={'symbol':symbol,'interval':interval,
                         'startTime':cur,'endTime':end_ts,'limit':1000},timeout=10).json()
        if not r: break
        rows.extend(r); cur = r[-1][0]+1
        time.sleep(0.25)
    cols=['ts','open','high','low','close','volume','ct','qv','tr','tbb','tbq','ign']
    df = pd.DataFrame(rows, columns=cols)
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    for c in ['open','high','low','close','volume']: df[c]=pd.to_numeric(df[c])
    df = df.set_index('ts')[['open','high','low','close','volume']]
    df.to_csv(csv); print(f'  {symbol}: saved {len(df)} candles')
    return df

def ema(s,n): return s.ewm(span=n,adjust=False).mean()
def macd(s):
    m=ema(s,12)-ema(s,26); return m,ema(m,9)
def calc_adx(df,p=14):
    hi,lo,cl=df['high'],df['low'],df['close']
    tr=pd.concat([hi-lo,(hi-cl.shift(1)).abs(),(lo-cl.shift(1)).abs()],axis=1).max(axis=1)
    atr=tr.rolling(p).mean()
    up=hi-hi.shift(1); dn=lo.shift(1)-lo
    pdm=pd.Series(np.where((up>dn)&(up>0),up,0),index=df.index)
    ndm=pd.Series(np.where((dn>up)&(dn>0),dn,0),index=df.index)
    pdi=100*pdm.rolling(p).mean()/atr
    ndi=100*ndm.rolling(p).mean()/atr
    dx=100*(pdi-ndi).abs()/(pdi+ndi)
    return dx.rolling(p).mean(),pdi,ndi

def build_signals(df):
    ef=ema(df['close'],9); es=ema(df['close'],21); et=ema(df['close'],200)
    m,ms=macd(df['close'])
    adx4,pdi,ndi=calc_adx(df)
    vol_ma=df['volume'].rolling(20).mean()
    vol_r=df['volume']/vol_ma
    slope=(et-et.shift(10))/et.shift(10)*100
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
    buy=((ef>es)&(ef.shift(1)<=es.shift(1))&(m>ms)&(df['close']>et)
         &(adx4>20)&(pdi>ndi)&(vol_r>0.8)&(slope<0.5)
         &(df['close']>e200d_h)&(adxd_h>20)&(distd_h<30))
    sell=(ef<es)&(ef.shift(1)>=es.shift(1))
    return buy.astype(int), sell.astype(int)

# ── SINGLE PAIR BACKTEST ──────────────────────────────────────
def backtest_single(df, capital=10.0, fee=FEE, stop_pct=STOP):
    buy_sig, sell_sig = build_signals(df)
    cash,pos,trades,equity = capital,None,[],[]
    for i in range(len(df)):
        row=df.iloc[i]; b=buy_sig.iloc[i]; s=sell_sig.iloc[i]
        if pos and row['close']<=pos['stop']:
            pr=pos['qty']*row['close']*(1-fee)
            trades.append({'pnl':pr-pos['cost'],'t':'sl'}); cash+=pr; pos=None
        total=cash+(pos['qty']*row['close'] if pos else 0)
        if b and pos is None and total>=5:
            qty=cash*(1-fee)/row['close']
            pos={'entry':row['close'],'qty':qty,'cost':cash,'stop':row['close']*(1-stop_pct)}
            cash=0
        elif s and pos:
            pr=pos['qty']*row['close']*(1-fee)
            trades.append({'pnl':pr-pos['cost'],'t':'sig'}); cash+=pr; pos=None
        equity.append(cash+(pos['qty']*row['close'] if pos else 0))
    if pos:
        lp=df.iloc[-1]['close']; pr=pos['qty']*lp*(1-fee)
        trades.append({'pnl':pr-pos['cost'],'t':'end'}); cash+=pr
    wins=[t for t in trades if t['pnl']>0]; loss=[t for t in trades if t['pnl']<=0]
    eq=pd.Series(equity,index=df.index); dd=(eq-eq.cummax())/eq.cummax()*100
    tw=sum(t['pnl'] for t in wins); tl=abs(sum(t['pnl'] for t in loss))
    return {'final':equity[-1],'ret':(equity[-1]-capital)/capital*100,
            'tr':len(trades),'wr':len(wins)/len(trades)*100 if trades else 0,
            'dd':dd.min(),'pf':tw/tl if tl>0 else 99,
            'sl':len([t for t in trades if t['t']=='sl'])}

# ── MULTI-PAIR BACKTEST ───────────────────────────────────────
# One position at a time. Capital goes to first signal that fires.
# When closed, capital available for any pair again.
def backtest_multi(pair_dfs, capital=10.0, fee=FEE, stop_pct=STOP):
    # build signals for each pair
    signals = {}
    for sym, df in pair_dfs.items():
        b, s = build_signals(df)
        signals[sym] = {'buy': b, 'sell': s, 'df': df}

    # align all pairs to common timestamps
    all_idx = sorted(set().union(*[set(df.index) for df in pair_dfs.values()]))
    all_idx = pd.DatetimeIndex(all_idx)

    cash = capital
    pos  = None   # {'sym', 'entry', 'qty', 'cost', 'stop'}
    trades = []
    equity = []
    trade_log = []

    for ts in all_idx:
        # check stop loss on current position
        if pos:
            sym = pos['sym']
            if ts in signals[sym]['df'].index:
                price = signals[sym]['df'].loc[ts,'close']
                if price <= pos['stop']:
                    pr = pos['qty']*price*(1-fee)
                    pnl = pr-pos['cost']
                    trades.append({'pnl':pnl,'sym':sym,'t':'sl'})
                    trade_log.append(f'STOP  {sym} @ ${price:,.0f}  pnl=${pnl:+.4f}')
                    cash+=pr; pos=None

        # check sell signal on current position
        if pos:
            sym = pos['sym']
            if ts in signals[sym]['df'].index:
                s_sig = signals[sym]['sell'].get(ts,0)
                price = signals[sym]['df'].loc[ts,'close']
                if s_sig:
                    pr = pos['qty']*price*(1-fee)
                    pnl = pr-pos['cost']
                    trades.append({'pnl':pnl,'sym':sym,'t':'sig'})
                    trade_log.append(f'SELL  {sym} @ ${price:,.0f}  pnl=${pnl:+.4f}')
                    cash+=pr; pos=None

        # look for buy signal on any pair (if no current position)
        if pos is None and cash >= 5:
            for sym, sig in signals.items():
                if ts in sig['df'].index and sig['buy'].get(ts,0):
                    price = sig['df'].loc[ts,'close']
                    qty   = cash*(1-fee)/price
                    pos   = {'sym':sym,'entry':price,'qty':qty,
                             'cost':cash,'stop':price*(1-stop_pct)}
                    trade_log.append(f'BUY   {sym} @ ${price:,.0f}')
                    cash  = 0
                    break   # one position at a time

        # track portfolio value
        val = cash
        if pos and pos['sym'] in signals:
            sym = pos['sym']
            if ts in signals[sym]['df'].index:
                val = pos['qty']*signals[sym]['df'].loc[ts,'close']
            else:
                val = pos['cost']  # use cost if no price at this ts
        equity.append(val)

    if pos:
        sym = pos['sym']
        lp  = signals[sym]['df']['close'].iloc[-1]
        pr  = pos['qty']*lp*(1-fee)
        trades.append({'pnl':pr-pos['cost'],'sym':sym,'t':'end'})
        cash+=pr

    wins=[t for t in trades if t['pnl']>0]; loss=[t for t in trades if t['pnl']<=0]
    eq=pd.Series(equity); dd=(eq-eq.cummax())/eq.cummax()*100
    tw=sum(t['pnl'] for t in wins); tl=abs(sum(t['pnl'] for t in loss))
    final=equity[-1] if equity else capital

    # trades per pair
    pair_counts = {sym:len([t for t in trades if t.get('sym')==sym]) for sym in pair_dfs}

    return {'final':final,'ret':(final-capital)/capital*100,
            'tr':len(trades),'wr':len(wins)/len(trades)*100 if trades else 0,
            'dd':dd.min(),'pf':tw/tl if tl>0 else 99,
            'sl':len([t for t in trades if t['t']=='sl']),
            'pair_counts':pair_counts,'trade_log':trade_log}

# ── MAIN ─────────────────────────────────────────────────────
if __name__ == '__main__':
    print('Loading data...')
    pair_dfs = {}
    for sym in PAIRS:
        try:
            pair_dfs[sym] = fetch(sym)
        except Exception as e:
            print(f'  {sym} failed: {e}')

    print()
    print('='*60)
    print('  SINGLE PAIR RESULTS (same strategy, one pair each)')
    print('='*60)
    print(f'  {"PAIR":<12} {"RETURN":>8} {"TRADES":>7} {"WIN%":>6} {"DD":>8} {"PF":>5}')
    print('  '+'-'*50)
    single_results = {}
    for sym, df in pair_dfs.items():
        try:
            r = backtest_single(df)
            single_results[sym] = r
            print(f'  {sym:<12} {r["ret"]:>7.1f}% {r["tr"]:>7}'
                  f' {r["wr"]:>5.1f}% {r["dd"]:>7.1f}% {r["pf"]:>5.2f}')
        except Exception as e:
            print(f'  {sym:<12} ERROR: {e}')

    print()
    print('='*60)
    print('  MULTI-PAIR COMBINED (one position at a time, any pair)')
    print('  Capital: $10  Fee: 0.1% each side  Stop: 8%')
    print('='*60)
    if len(pair_dfs) >= 2:
        mr = backtest_multi(pair_dfs)
        print(f'  Final capital   : ${mr["final"]:.4f}')
        print(f'  Total return    : {mr["ret"]:.2f}%')
        print(f'  Total trades    : {mr["tr"]}')
        print(f'  Win rate        : {mr["wr"]:.1f}%')
        print(f'  Max drawdown    : {mr["dd"]:.2f}%')
        print(f'  Profit factor   : {mr["pf"]:.2f}')
        print(f'  Stop losses     : {mr["sl"]}')
        print()
        print('  Trades per pair:')
        for sym, cnt in mr['pair_counts'].items():
            print(f'    {sym:<12}: {cnt} trades')
        print()
        print('  TRADE LOG (all trades in order):')
        for entry in mr['trade_log']:
            print(f'    {entry}')

        print()
        print('='*60)
        print('  COMPARISON')
        print('='*60)
        btc_r = single_results.get('BTCUSDT',{})
        print(f'  BTC only        : {btc_r.get("ret","?"):.1f}% return, '
              f'{btc_r.get("tr","?")} trades')
        print(f'  All 4 pairs     : {mr["ret"]:.1f}% return, {mr["tr"]} trades')
        improvement = mr["ret"] / btc_r.get("ret", 1) if btc_r.get("ret",0) > 0 else 0
        print(f'  Return improved : {improvement:.1f}x')
        print(f'  Trades improved : {mr["tr"] / btc_r.get("tr",1):.1f}x more opportunities')
