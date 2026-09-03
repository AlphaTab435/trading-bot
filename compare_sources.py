"""
Data source comparison tool.
Fetches recent BTC 4h candles from multiple sources
and compares them against api.binance.com (the ground truth).

Run this on your local machine (which can access Binance.com).
It will tell you exactly which source is safest to use on GitHub Actions.
"""
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

SOURCES = {}

# ─────────────────────────────────────────
# FETCH FUNCTIONS
# ─────────────────────────────────────────

def fetch_binance_com(limit=100):
    """Global Binance — our ground truth."""
    try:
        r = requests.get('https://api.binance.com/api/v3/klines',
                        params={'symbol':'BTCUSDT','interval':'4h','limit':limit},
                        timeout=10)
        r.raise_for_status()
        df = pd.DataFrame(r.json(),
                         columns=['ts','open','high','low','close','volume',
                                  'ct','qv','tr','tbb','tbq','ign'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
        for c in ['open','high','low','close']:
            df[c] = pd.to_numeric(df[c])
        return df.set_index('ts')[['open','high','low','close']]
    except Exception as e:
        print(f'  binance.com FAILED: {e}')
        return None

def fetch_binance_us(limit=100):
    """Binance US — separate exchange, accessible from US IPs."""
    try:
        r = requests.get('https://api.binance.us/api/v3/klines',
                        params={'symbol':'BTCUSDT','interval':'4h','limit':limit},
                        timeout=10)
        r.raise_for_status()
        df = pd.DataFrame(r.json(),
                         columns=['ts','open','high','low','close','volume',
                                  'ct','qv','tr','tbb','tbq','ign'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
        for c in ['open','high','low','close']:
            df[c] = pd.to_numeric(df[c])
        return df.set_index('ts')[['open','high','low','close']]
    except Exception as e:
        print(f'  binance.us FAILED: {e}')
        return None

def fetch_kraken(limit=100):
    """
    Kraken — major global exchange, no geo-blocking anywhere.
    XBT = Bitcoin on Kraken (same as BTC)
    """
    try:
        r = requests.get('https://api.kraken.com/0/public/OHLC',
                        params={'pair':'XBTUSD','interval':240},
                        timeout=10)
        data = r.json()
        if data.get('error'):
            print(f'  kraken FAILED: {data["error"]}')
            return None
        rows = data['result']['XXBTZUSD']
        df = pd.DataFrame(rows, columns=['ts','open','high','low','close',
                                         'vwap','volume','count'])
        df['ts'] = pd.to_datetime(df['ts'].astype(int), unit='s', utc=True)
        for c in ['open','high','low','close']:
            df[c] = pd.to_numeric(df[c])
        df = df.set_index('ts')[['open','high','low','close']]
        return df.tail(limit)
    except Exception as e:
        print(f'  kraken FAILED: {e}')
        return None

def fetch_coinbase(limit=100):
    """
    Coinbase Advanced Trade — US exchange but API is globally accessible.
    """
    try:
        import time
        end   = int(time.time())
        start = end - (limit * 4 * 3600)
        r = requests.get('https://api.exchange.coinbase.com/products/BTC-USD/candles',
                        params={'granularity':14400,
                                'start': datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
                                'end':   datetime.fromtimestamp(end,   tz=timezone.utc).isoformat()},
                        timeout=10)
        r.raise_for_status()
        df = pd.DataFrame(r.json(),
                         columns=['ts','low','high','open','close','volume'])
        df['ts'] = pd.to_datetime(df['ts'], unit='s', utc=True)
        for c in ['open','high','low','close']:
            df[c] = pd.to_numeric(df[c])
        df = df.set_index('ts').sort_index()[['open','high','low','close']]
        return df.tail(limit)
    except Exception as e:
        print(f'  coinbase FAILED: {e}')
        return None

def fetch_bybit(limit=100):
    """
    Bybit — major global exchange, no US restrictions on public data.
    """
    try:
        r = requests.get('https://api.bybit.com/v5/market/kline',
                        params={'category':'spot','symbol':'BTCUSDT',
                                'interval':'240','limit':limit},
                        timeout=10)
        data = r.json()
        if data.get('retCode') != 0:
            print(f'  bybit FAILED: {data.get("retMsg")}')
            return None
        rows = data['result']['list']
        df = pd.DataFrame(rows, columns=['ts','open','high','low','close','volume','turnover'])
        df['ts'] = pd.to_datetime(df['ts'].astype(int), unit='ms', utc=True)
        for c in ['open','high','low','close']:
            df[c] = pd.to_numeric(df[c])
        df = df.set_index('ts').sort_index()[['open','high','low','close']]
        return df.tail(limit)
    except Exception as e:
        print(f'  bybit FAILED: {e}')
        return None


# ─────────────────────────────────────────
# COMPARISON ENGINE
# ─────────────────────────────────────────
def compare(base, other, name):
    """Compare another source against Binance.com on matching timestamps."""
    common = base.index.intersection(other.index)
    if len(common) < 5:
        return {'name': name, 'error': f'Only {len(common)} matching timestamps'}

    b = base.loc[common, 'close']
    o = other.loc[common, 'close']

    diff_pct    = ((o - b) / b * 100).abs()
    correlation = b.corr(o)

    return {
        'name':         name,
        'matching':     len(common),
        'avg_diff_pct': diff_pct.mean(),
        'max_diff_pct': diff_pct.max(),
        'p95_diff_pct': diff_pct.quantile(0.95),
        'correlation':  correlation,
        'accessible':   True,
    }


# ─────────────────────────────────────────
# SIGNAL CHECK — does different data change signals?
# ─────────────────────────────────────────
def ema(s,n): return s.ewm(span=n,adjust=False).mean()

def would_signal_differ(base_df, other_df):
    """Check if using different source changes the BUY/SELL signal."""
    diffs = 0
    total = 0
    common = base_df.index.intersection(other_df.index)
    if len(common) < 50:
        return None

    for df, label in [(base_df.loc[common], 'base'), (other_df.loc[common], 'other')]:
        e9  = ema(df['close'], 9)
        e21 = ema(df['close'], 21)
        cross = (e9 > e21) & (e9.shift(1) <= e21.shift(1))
        if label == 'base':
            base_cross = cross
        else:
            other_cross = cross

    # count candles where signal differs
    differ = (base_cross != other_cross).sum()
    pct    = differ / len(common) * 100
    return differ, len(common), pct


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
if __name__ == '__main__':
    print('Fetching data from all sources...\n')

    sources = {
        'binance.com (GROUND TRUTH)': fetch_binance_com,
        'binance.us':                 fetch_binance_us,
        'kraken':                     fetch_kraken,
        'coinbase':                   fetch_coinbase,
        'bybit':                      fetch_bybit,
    }

    data = {}
    for name, fn in sources.items():
        print(f'  Fetching {name}...')
        df = fn(limit=100)
        if df is not None:
            data[name] = df
            latest = df['close'].iloc[-1]
            print(f'    OK — {len(df)} candles, latest close: ${latest:,.2f}')
        else:
            print(f'    FAILED or blocked')

    if 'binance.com (GROUND TRUTH)' not in data:
        print('\nERROR: Cannot reach api.binance.com from this machine.')
        print('This script must be run on a machine that can access Binance.com')
        exit(1)

    base = data['binance.com (GROUND TRUTH)']

    print()
    print('='*72)
    print('  PRICE ACCURACY vs api.binance.com (ground truth)')
    print('='*72)
    print(f'  {"SOURCE":<25} {"CANDLES":>8} {"AVG DIFF":>10} '
          f'{"MAX DIFF":>10} {"95th":>8} {"CORR":>8}')
    print('  '+'-'*70)

    results = []
    for name, df in data.items():
        if name == 'binance.com (GROUND TRUTH)':
            print(f'  {"binance.com":<25} {len(df):>8}  {"BASELINE":>10}')
            continue
        r = compare(base, df, name)
        results.append(r)
        if 'error' in r:
            print(f'  {name:<25}  {r["error"]}')
        else:
            flag = ''
            if r['avg_diff_pct'] < 0.1: flag = ' <<< EXCELLENT'
            elif r['avg_diff_pct'] < 0.5: flag = ' << GOOD'
            elif r['avg_diff_pct'] < 1.0: flag = ' < ACCEPTABLE'
            else: flag = ' WARNING: HIGH DIFF'
            print(f'  {name:<25} {r["matching"]:>8} {r["avg_diff_pct"]:>9.3f}%'
                  f' {r["max_diff_pct"]:>9.3f}% {r["p95_diff_pct"]:>7.3f}%'
                  f' {r["correlation"]:>8.6f}{flag}')

    print()
    print('='*72)
    print('  SIGNAL ACCURACY — does using different data change BUY/SELL signals?')
    print('  (Using EMA 9/21 crossover — our entry/exit signal)')
    print('='*72)
    for name, df in data.items():
        if name == 'binance.com (GROUND TRUTH)': continue
        result = would_signal_differ(base, df)
        if result is None:
            print(f'  {name:<25}: not enough matching candles')
        else:
            differ, total, pct = result
            quality = 'IDENTICAL' if differ == 0 else (
                      'NEAR IDENTICAL' if pct < 1 else
                      'MOSTLY SAME' if pct < 5 else 'DIFFERENT')
            print(f'  {name:<25}: {differ} of {total} candles had different signal '
                  f'({pct:.1f}%)  [{quality}]')

    print()
    print('='*72)
    print('  ACCESSIBILITY FROM GITHUB ACTIONS (US servers)')
    print('='*72)
    test_urls = {
        'api.binance.com':   'https://api.binance.com/api/v3/ping',
        'api.binance.us':    'https://api.binance.us/api/v3/ping',
        'api.kraken.com':    'https://api.kraken.com/0/public/Time',
        'api.exchange.coinbase.com': 'https://api.exchange.coinbase.com/time',
        'api.bybit.com':     'https://api.bybit.com/v5/market/time',
    }
    print('  (Note: these tests run from YOUR machine, not GitHub.')
    print('   Binance.com will likely show OK here but fails on GitHub)')
    for name, url in test_urls.items():
        try:
            r = requests.get(url, timeout=5)
            status = f'OK ({r.status_code})'
        except Exception as e:
            status = f'FAILED ({str(e)[:40]})'
        print(f'  {name:<35}: {status}')

    print()
    print('='*72)
    print('  RECOMMENDATION')
    print('='*72)
    valid = [r for r in results if 'error' not in r]
    if valid:
        best = min(valid, key=lambda x: x['avg_diff_pct'])
        print(f'\n  Best alternative to api.binance.com: {best["name"]}')
        print(f'  Average price difference: {best["avg_diff_pct"]:.3f}%')
        print(f'  This means: on a $77,000 BTC price, the typical difference')
        print(f'  is ${77000 * best["avg_diff_pct"] / 100:.0f}')
        print(f'  Our stop loss is 8% = ${77000 * 0.08:,.0f}')
        print(f'  Our typical winning trade is ~14% = ${77000 * 0.14:,.0f}')
        print(f'  A {best["avg_diff_pct"]:.3f}% data difference is'
              f' {"NEGLIGIBLE" if best["avg_diff_pct"] < 0.5 else "WORTH NOTING"}'
              f' at this scale.')
