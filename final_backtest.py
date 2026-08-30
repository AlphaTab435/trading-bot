"""
FINAL BACKTEST — BTC/USDT 4h
Strategy: EMA cross + MACD + 200 EMA + ADX + DI + Volume + MTF daily
Run: python final_backtest.py
"""
import requests, time, os
import pandas as pd
import numpy as np
from datetime import datetime

# ─────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────
SYMBOL      = 'BTCUSDT'
INTERVAL    = '4h'
START_DATE  = '2021-01-01'
CAPITAL     = 10.0
FEE         = 0.001   # 0.1% each side
STOP_PCT    = 0.08    # 8% stop loss
CSV_FILE    = 'btcusdt_4h.csv'

# strategy parameters
EMA_FAST         = 9
EMA_SLOW         = 21
EMA_TREND        = 200
ADX_MIN          = 20
VOL_MIN_RATIO    = 0.8
SLOPE_MAX        = 0.5    # max 4h 200 EMA slope % per 10 candles
DAILY_DIST_MAX   = 30.0   # max % above daily 200 EMA


# ─────────────────────────────────────────
# DATA
# ─────────────────────────────────────────
def fetch_binance(symbol, interval, start_str, end_str=None):
    url = 'https://api.binance.com/api/v3/klines'
    if end_str is None:
        end_str = datetime.utcnow().strftime('%Y-%m-%d')
    start_ts = int(datetime.strptime(start_str, '%Y-%m-%d').timestamp() * 1000)
    end_ts   = int(datetime.strptime(end_str,   '%Y-%m-%d').timestamp() * 1000)
    rows, cur = [], start_ts
    while cur < end_ts:
        resp = requests.get(url, params={'symbol': symbol, 'interval': interval,
                            'startTime': cur, 'endTime': end_ts, 'limit': 1000},
                            timeout=10).json()
        if not resp: break
        rows.extend(resp)
        cur = resp[-1][0] + 1
        print(f'  {len(rows)} candles fetched...')
        time.sleep(0.3)
    cols = ['ts','open','high','low','close','volume',
            'ct','qv','tr','tbb','tbq','ign']
    df = pd.DataFrame(rows, columns=cols)
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c])
    return df.set_index('ts')[['open','high','low','close','volume']]

def load_data():
    if os.path.exists(CSV_FILE):
        df = pd.read_csv(CSV_FILE, index_col=0, parse_dates=True)
        last = df.index[-1]
        days_old = (pd.Timestamp.now() - last.tz_localize(None) if last.tzinfo else pd.Timestamp.now() - last).days
        if days_old > 3:
            print(f'Data ends {last.date()} ({days_old} days ago). Fetching update...')
            try:
                new_start = (last + pd.Timedelta(hours=4)).strftime('%Y-%m-%d')
                new = fetch_binance(SYMBOL, INTERVAL, new_start)
                df = pd.concat([df, new]).pipe(lambda x: x[~x.index.duplicated()])
                df.to_csv(CSV_FILE)
                print(f'Updated to {df.index[-1].date()}')
            except Exception as e:
                print(f'Update failed ({e}). Using existing data.')
        else:
            print(f'Loaded {CSV_FILE} ({len(df)} candles, up to {last.date()})')
    else:
        print(f'Downloading {SYMBOL} {INTERVAL} data...')
        df = fetch_binance(SYMBOL, INTERVAL, START_DATE)
        df.to_csv(CSV_FILE)
    return df


# ─────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────
def calc_ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def calc_macd(s):
    m = calc_ema(s, 12) - calc_ema(s, 26)
    return m, calc_ema(m, 9)

def calc_adx(df, p=14):
    hi, lo, cl = df['high'], df['low'], df['close']
    tr = pd.concat([hi-lo, (hi-cl.shift(1)).abs(),
                    (lo-cl.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(p).mean()
    up = hi - hi.shift(1); dn = lo.shift(1) - lo
    pdm = pd.Series(np.where((up>dn)&(up>0), up, 0), index=df.index)
    ndm = pd.Series(np.where((dn>up)&(dn>0), dn, 0), index=df.index)
    pdi = 100 * pdm.rolling(p).mean() / atr
    ndi = 100 * ndm.rolling(p).mean() / atr
    dx  = 100 * (pdi-ndi).abs() / (pdi+ndi)
    return dx.rolling(p).mean(), pdi, ndi

def calc_rsi(s, p=14):
    d = s.diff()
    g = d.where(d>0, 0).rolling(p).mean()
    l = (-d.where(d<0, 0)).rolling(p).mean()
    return 100 - (100 / (1 + g/l))

def build_indicators(df):
    ind = {}
    ind['e_fast']  = calc_ema(df['close'], EMA_FAST)
    ind['e_slow']  = calc_ema(df['close'], EMA_SLOW)
    ind['e_trend'] = calc_ema(df['close'], EMA_TREND)
    ind['macd'], ind['macd_sig'] = calc_macd(df['close'])
    ind['adx'], ind['pdi'], ind['ndi'] = calc_adx(df)
    ind['vol_ma']    = df['volume'].rolling(20).mean()
    ind['vol_ratio'] = df['volume'] / ind['vol_ma']
    ind['slope200']  = (ind['e_trend'] - ind['e_trend'].shift(10)) / ind['e_trend'].shift(10) * 100

    # daily resampled indicators
    daily = df['close'].resample('D').last().dropna().to_frame()
    daily['high']   = df['high'].resample('D').max()
    daily['low']    = df['low'].resample('D').min()
    daily['volume'] = df['volume'].resample('D').sum()
    e200d = calc_ema(daily['close'], 200)
    adxd, pdid, ndid = calc_adx(daily)
    dist_d = (daily['close'] - e200d) / e200d * 100
    ind['e200d']  = e200d.reindex(df.index,  method='ffill')
    ind['adxd']   = adxd.reindex(df.index,   method='ffill')
    ind['dist_d'] = dist_d.reindex(df.index, method='ffill')
    return ind

def build_signals(df, ind):
    ef, es, et = ind['e_fast'], ind['e_slow'], ind['e_trend']
    cross_up   = (ef > es) & (ef.shift(1) <= es.shift(1))
    cross_dn   = (ef < es) & (ef.shift(1) >= es.shift(1))

    buy = (cross_up
           & (ind['macd'] > ind['macd_sig'])   # MACD confirms
           & (df['close'] > et)                # above 200 EMA
           & (ind['adx'] > ADX_MIN)            # trend strong enough
           & (ind['pdi'] > ind['ndi'])         # bulls in control
           & (ind['vol_ratio'] > VOL_MIN_RATIO)# volume confirms
           & (ind['slope200'] < SLOPE_MAX)     # trend not overheating
           & (df['close'] > ind['e200d'])      # above daily 200 EMA
           & (ind['adxd'] > ADX_MIN)           # daily trend strong
           & (ind['dist_d'] < DAILY_DIST_MAX)) # not overextended daily

    sell = cross_dn
    return buy.astype(int), sell.astype(int)


# ─────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────
def run_backtest(df, buy_sig, sell_sig, capital=CAPITAL,
                 fee=FEE, stop_pct=STOP_PCT):
    BINANCE_MIN = 5.0
    cash, pos, trades, equity = capital, None, [], []

    for i in range(len(df)):
        row   = df.iloc[i]
        ts    = row.name
        b_sig = buy_sig.iloc[i]
        s_sig = sell_sig.iloc[i]

        # check stop loss
        if pos and row['close'] <= pos['stop']:
            proceeds = pos['qty'] * row['close'] * (1 - fee)
            trades.append({'type':'stop','date':ts,
                           'entry':pos['entry'],'exit':row['close'],
                           'pnl':proceeds - pos['cost']})
            cash += proceeds
            pos = None

        total = cash + (pos['qty'] * row['close'] if pos else 0)

        # buy
        if b_sig and pos is None and total >= BINANCE_MIN:
            qty = cash * (1 - fee) / row['close']
            pos = {'entry': row['close'], 'qty': qty,
                   'cost': cash, 'stop': row['close'] * (1 - stop_pct),
                   'date': ts}
            cash = 0.0

        # sell
        elif s_sig and pos:
            proceeds = pos['qty'] * row['close'] * (1 - fee)
            trades.append({'type':'signal','date':ts,
                           'entry':pos['entry'],'exit':row['close'],
                           'pnl':proceeds - pos['cost']})
            cash += proceeds
            pos = None

        equity.append(cash + (pos['qty'] * row['close'] if pos else 0))

    # close open position at end
    if pos:
        lp = df.iloc[-1]['close']
        pr = pos['qty'] * lp * (1 - fee)
        trades.append({'type':'end','date':df.index[-1],
                       'entry':pos['entry'],'exit':lp,
                       'pnl':pr - pos['cost']})
        cash += pr

    wins   = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    eq     = pd.Series(equity, index=df.index)
    dd     = (eq - eq.cummax()) / eq.cummax() * 100
    tw = sum(t['pnl'] for t in wins)
    tl = abs(sum(t['pnl'] for t in losses))

    return {
        'final':   equity[-1],
        'trades':  trades,
        'equity':  eq,
        'wins':    wins,
        'losses':  losses,
        'metrics': {
            'return_pct':    (equity[-1] - capital) / capital * 100,
            'total_trades':  len(trades),
            'win_rate':      len(wins) / len(trades) * 100 if trades else 0,
            'profit_factor': tw / tl if tl > 0 else float('inf'),
            'max_drawdown':  dd.min(),
            'avg_win':       np.mean([t['pnl'] for t in wins]) if wins else 0,
            'avg_loss':      np.mean([t['pnl'] for t in losses]) if losses else 0,
            'stop_losses':   len([t for t in trades if t['type'] == 'stop']),
            'min_equity':    eq.min(),
            'max_equity':    eq.max(),
            'cagr':          ((equity[-1] / capital) ** (1 / 5.7) - 1) * 100,
        }
    }


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
if __name__ == '__main__':
    df  = load_data()
    print(f'Range: {df.index[0].date()} → {df.index[-1].date()}  ({len(df)} candles)\n')

    ind            = build_indicators(df)
    buy_sig, sell_sig = build_signals(df, ind)
    result         = run_backtest(df, buy_sig, sell_sig)
    m              = result['metrics']

    print('=' * 58)
    print('  FINAL STRATEGY BACKTEST RESULTS')
    print('=' * 58)
    print(f'  Starting capital   : ${CAPITAL:.2f}')
    print(f'  Final capital      : ${result["final"]:.4f}')
    print(f'  Total return       : {m["return_pct"]:.2f}%')
    print(f'  CAGR               : {m["cagr"]:.1f}% per year')
    print()
    print(f'  Total trades       : {m["total_trades"]}')
    print(f'  Win rate           : {m["win_rate"]:.1f}%')
    print(f'  Profit factor      : {m["profit_factor"]:.2f}')
    print(f'  Avg win            : ${m["avg_win"]:.4f}')
    print(f'  Avg loss           : ${m["avg_loss"]:.4f}')
    print()
    print(f'  Max drawdown       : {m["max_drawdown"]:.2f}%')
    print(f'  Lowest portfolio   : ${m["min_equity"]:.4f}')
    print(f'  Highest portfolio  : ${m["max_equity"]:.4f}')
    print(f'  Stop losses hit    : {m["stop_losses"]}')
    print()
    print('  TRADE LOG:')
    for t in result['trades']:
        pnl_str = f'+${t["pnl"]:.4f}' if t['pnl'] > 0 else f'-${abs(t["pnl"]):.4f}'
        print(f'  {pd.Timestamp(t["date"]).date()}  '
              f'entry=${t["entry"]:,.0f}  exit=${t["exit"]:,.0f}  '
              f'{pnl_str}  [{t["type"]}]')
    print('=' * 58)
