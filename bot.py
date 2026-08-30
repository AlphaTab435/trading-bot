"""
TRADING BOT - GitHub Actions version
Runs once per execution, saves state to state.json
Designed to run via GitHub Actions cron every 4 hours

Paper mode: set PAPER_MODE = True (no API keys needed)
Live mode:  set PAPER_MODE = False, add API keys as GitHub Secrets
"""
import requests, time, json, os, sys, hmac, hashlib, urllib.parse
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ─────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────
PAPER_MODE           = True
PAPER_STARTING_BAL   = 10.0
SYMBOL               = 'BTCUSDT'
STOP_LOSS_PCT        = 0.08
FEE                  = 0.001
BINANCE_MIN_NOTIONAL = 5.0
STATE_FILE           = 'state.json'

# Read API keys from environment variables (GitHub Secrets)
API_KEY    = os.environ.get('BINANCE_API_KEY', '')
API_SECRET = os.environ.get('BINANCE_API_SECRET', '')

# Strategy parameters
EMA_FAST       = 9
EMA_SLOW       = 21
EMA_TREND      = 200
ADX_MIN        = 20
VOL_MIN_RATIO  = 0.8
SLOPE_MAX      = 0.5
DAILY_DIST_MAX = 30.0


# ─────────────────────────────────────────
# STATE (reads/writes state.json)
# ─────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    # first run defaults
    return {
        'paper_balance': PAPER_STARTING_BAL,
        'position':      None,
        'trades':        [],
        'last_run':      None,
    }

def save_state(state):
    state['last_run'] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    print(f'State saved to {STATE_FILE}')


# ─────────────────────────────────────────
# BINANCE DATA & API
# ─────────────────────────────────────────
def fetch_klines(symbol, interval, limit=300):
    url  = 'https://api.binance.com/api/v3/klines'
    resp = requests.get(url,
                        params={'symbol': symbol, 'interval': interval,
                                'limit': limit},
                        timeout=15)
    resp.raise_for_status()
    cols = ['ts','open','high','low','close','volume',
            'ct','qv','tr','tbb','tbq','ign']
    df = pd.DataFrame(resp.json(), columns=cols)
    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c])
    return df.set_index('ts')[['open','high','low','close','volume']]

def sign_request(params):
    qs  = urllib.parse.urlencode(params)
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return sig

def binance_post(endpoint, params):
    params['timestamp'] = int(time.time() * 1000)
    params['signature'] = sign_request(params)
    resp = requests.post(
        f'https://api.binance.com/api/v3/{endpoint}',
        headers={'X-MBX-APIKEY': API_KEY},
        params=params, timeout=15)
    return resp.json()

def get_account_balance(asset='USDT'):
    params = {'timestamp': int(time.time()*1000)}
    params['signature'] = sign_request(params)
    resp = requests.get('https://api.binance.com/api/v3/account',
                        headers={'X-MBX-APIKEY': API_KEY},
                        params=params, timeout=15)
    for a in resp.json().get('balances', []):
        if a['asset'] == asset:
            return float(a['free'])
    return 0.0

def cancel_open_orders(symbol):
    """Cancel all open orders for symbol (clears old stop orders)."""
    params = {'symbol': symbol}
    params['timestamp'] = int(time.time()*1000)
    params['signature'] = sign_request(params)
    requests.delete('https://api.binance.com/api/v3/openOrders',
                    headers={'X-MBX-APIKEY': API_KEY},
                    params=params, timeout=15)


# ─────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────
def ema(s, n):  return s.ewm(span=n, adjust=False).mean()
def macd(s):
    m = ema(s,12)-ema(s,26); return m, ema(m,9)
def calc_adx(df, p=14):
    hi,lo,cl = df['high'], df['low'], df['close']
    tr  = pd.concat([hi-lo,(hi-cl.shift(1)).abs(),
                     (lo-cl.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(p).mean()
    up  = hi-hi.shift(1); dn = lo.shift(1)-lo
    pdm = pd.Series(np.where((up>dn)&(up>0), up, 0), index=df.index)
    ndm = pd.Series(np.where((dn>up)&(dn>0), dn, 0), index=df.index)
    pdi = 100*pdm.rolling(p).mean()/atr
    ndi = 100*ndm.rolling(p).mean()/atr
    dx  = 100*(pdi-ndi).abs()/(pdi+ndi)
    return dx.rolling(p).mean(), pdi, ndi


# ─────────────────────────────────────────
# SIGNAL ENGINE
# ─────────────────────────────────────────
def compute_signal(df4h):
    daily = df4h['close'].resample('D').last().dropna().to_frame()
    daily['high']   = df4h['high'].resample('D').max()
    daily['low']    = df4h['low'].resample('D').min()
    daily['volume'] = df4h['volume'].resample('D').sum()

    ef  = ema(df4h['close'], EMA_FAST)
    es  = ema(df4h['close'], EMA_SLOW)
    et  = ema(df4h['close'], EMA_TREND)
    m4, ms4 = macd(df4h['close'])
    adx4, pdi4, ndi4 = calc_adx(df4h)
    vol_ma   = df4h['volume'].rolling(20).mean()
    vol_r    = df4h['volume'] / vol_ma
    slope200 = (et - et.shift(10)) / et.shift(10) * 100

    e200d = ema(daily['close'], 200)
    adxd, _, _ = calc_adx(daily)
    dist_d = (daily['close'] - e200d) / e200d * 100
    e200d_h = e200d.reindex(df4h.index, method='ffill')
    adxd_h  = adxd.reindex(df4h.index,  method='ffill')
    dist_h  = dist_d.reindex(df4h.index, method='ffill')

    i = -2
    price = df4h['close'].iloc[i]
    candle = df4h.index[i]

    cross_up = (ef.iloc[i] > es.iloc[i]) and (ef.iloc[i-1] <= es.iloc[i-1])
    cross_dn = (ef.iloc[i] < es.iloc[i]) and (ef.iloc[i-1] >= es.iloc[i-1])

    conditions = {
        'EMA cross up    ': cross_up,
        'MACD positive   ': m4.iloc[i] > ms4.iloc[i],
        'Above 4h 200EMA ': price > et.iloc[i],
        'ADX > 20        ': adx4.iloc[i] > ADX_MIN,
        '+DI > -DI       ': pdi4.iloc[i] > ndi4.iloc[i],
        'Volume >= 0.8x  ': vol_r.iloc[i] >= VOL_MIN_RATIO,
        'EMA slope < 0.5%': slope200.iloc[i] < SLOPE_MAX,
        'Above daily 200 ': price > e200d_h.iloc[i],
        'Daily ADX > 20  ': adxd_h.iloc[i] > ADX_MIN,
        'Not overextended': dist_h.iloc[i] < DAILY_DIST_MAX,
    }

    yes = sum(conditions.values())
    return ('BUY' if all(conditions.values()) else
            'SELL' if cross_dn else None), price, candle, conditions, yes


# ─────────────────────────────────────────
# TRADE EXECUTION
# ─────────────────────────────────────────
def paper_buy(state, price):
    bal = state['paper_balance']
    if bal < BINANCE_MIN_NOTIONAL:
        print(f'Balance ${bal:.4f} below minimum. Skip.')
        return
    qty   = bal * (1-FEE) / price
    stop  = round(price * (1-STOP_LOSS_PCT), 2)
    state['position'] = {
        'entry': price, 'qty': qty, 'cost': bal,
        'stop': stop, 'date': datetime.now(timezone.utc).isoformat()
    }
    state['paper_balance'] = 0.0
    print(f'[PAPER BUY]  {qty:.8f} BTC @ ${price:,.2f}  stop=${stop:,.2f}')

def paper_sell(state, price, reason):
    pos  = state['position']
    if not pos: return
    proc = pos['qty'] * price * (1-FEE)
    pnl  = proc - pos['cost']
    state['paper_balance'] = proc
    state['position']      = None
    state['trades'].append({
        'date': datetime.now(timezone.utc).isoformat(),
        'entry': pos['entry'], 'exit': price,
        'pnl': round(pnl, 6), 'reason': reason
    })
    print(f'[PAPER SELL] @ ${price:,.2f}  pnl=${pnl:+.4f}  reason={reason}')

def live_buy(state, price, df4h):
    """Place market buy + stop-limit order on Binance."""
    bal = get_account_balance('USDT')
    if bal < BINANCE_MIN_NOTIONAL:
        print(f'USDT balance ${bal:.4f} too low.'); return

    # market buy
    qty = round(bal * (1-FEE) / price, 6)
    res = binance_post('order', {
        'symbol': SYMBOL, 'side': 'BUY',
        'type': 'MARKET', 'quantity': qty
    })
    if 'orderId' not in res:
        print(f'Buy failed: {res}'); return

    filled = float(res.get('fills',[{}])[0].get('price', price))
    stop   = round(filled * (1-STOP_LOSS_PCT), 2)
    limit  = round(filled * (1-STOP_LOSS_PCT - 0.002), 2)  # 0.2% below stop

    # cancel any existing stop orders
    cancel_open_orders(SYMBOL)

    # place stop-limit order — Binance monitors this even when bot is off
    stop_res = binance_post('order', {
        'symbol': SYMBOL, 'side': 'SELL',
        'type': 'STOP_LOSS_LIMIT',
        'timeInForce': 'GTC',
        'quantity': qty,
        'stopPrice': stop,
        'price': limit
    })

    state['position'] = {
        'entry': filled, 'qty': qty,
        'cost': bal, 'stop': stop,
        'stop_order_id': stop_res.get('orderId'),
        'date': datetime.now(timezone.utc).isoformat()
    }
    print(f'[LIVE BUY]  {qty} BTC @ ${filled:,.2f}')
    print(f'            Stop-limit order placed at ${stop:,.2f} (order #{stop_res.get("orderId")})')
    print(f'            Binance will execute stop even if bot is offline.')

def live_sell(state, reason):
    """Cancel stop order and place market sell."""
    pos = state['position']
    if not pos: return
    cancel_open_orders(SYMBOL)
    res = binance_post('order', {
        'symbol': SYMBOL, 'side': 'SELL',
        'type': 'MARKET', 'quantity': pos['qty']
    })
    if 'orderId' not in res:
        print(f'Sell failed: {res}'); return
    price = float(res.get('fills',[{}])[0].get('price', 0))
    pnl   = (pos['qty'] * price * (1-FEE)) - pos['cost']
    state['position'] = None
    state['trades'].append({
        'date': datetime.now(timezone.utc).isoformat(),
        'entry': pos['entry'], 'exit': price,
        'pnl': round(pnl, 6), 'reason': reason
    })
    print(f'[LIVE SELL] @ ${price:,.2f}  pnl=${pnl:+.4f}  reason={reason}')


# ─────────────────────────────────────────
# STATUS SUMMARY
# ─────────────────────────────────────────
def print_summary(state, current_price, signal, yes_count, conditions):
    pos   = state['position']
    bal   = state['paper_balance'] if PAPER_MODE else get_account_balance()
    wins  = [t for t in state['trades'] if t['pnl'] > 0]
    total_pnl = sum(t['pnl'] for t in state['trades'])
    mode  = 'PAPER' if PAPER_MODE else 'LIVE'

    print()
    print(f'========== BOT RUN {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")} ({mode}) ==========')
    print(f'BTC price      : ${current_price:,.2f}')
    if pos:
        unr = (current_price - pos['entry']) / pos['entry'] * 100
        dst = (current_price - pos['stop'])  / current_price * 100
        print(f'POSITION       : OPEN  entry=${pos["entry"]:,.2f}')
        print(f'Stop loss      : ${pos["stop"]:,.2f}  ({dst:.1f}% away)')
        print(f'Unrealised P&L : {unr:+.2f}%')
        if not PAPER_MODE:
            print(f'Stop order     : #{pos.get("stop_order_id","?")} on Binance (active when bot offline)')
    else:
        print(f'POSITION       : None  |  Balance: ${bal:.4f}')
    print(f'Total trades   : {len(state["trades"])}  |  Wins: {len(wins)}  |  P&L: ${total_pnl:+.4f}')
    print()
    print(f'SIGNAL CHECK ({yes_count}/10 conditions met):')
    for name, val in conditions.items():
        print(f'  {"YES" if val else "NO "} {name}')
    print()
    if signal == 'BUY':
        print('>>> BUY SIGNAL <<<')
    elif signal == 'SELL':
        print('>>> SELL SIGNAL <<<')
    else:
        missing = [n.strip() for n,v in conditions.items() if not v]
        print(f'No signal. Missing: {", ".join(missing)}')
    print('=' * 60)


# ─────────────────────────────────────────
# MAIN — runs once per execution
# ─────────────────────────────────────────
def main():
    print(f'Bot run started: {datetime.now(timezone.utc).isoformat()}')

    state = load_state()
    df4h  = fetch_klines(SYMBOL, '4h', limit=300)

    current_price = float(df4h['close'].iloc[-1])
    signal, sig_price, candle, conditions, yes_count = compute_signal(df4h)

    # check if Binance already executed stop-limit (live mode)
    if not PAPER_MODE and state['position']:
        order_id = state['position'].get('stop_order_id')
        if order_id:
            params = {'symbol': SYMBOL, 'orderId': order_id,
                      'timestamp': int(time.time()*1000)}
            params['signature'] = sign_request(params)
            r = requests.get('https://api.binance.com/api/v3/order',
                             headers={'X-MBX-APIKEY': API_KEY},
                             params=params, timeout=15).json()
            if r.get('status') == 'FILLED':
                print(f'Stop-limit order was filled by Binance while bot was offline.')
                stop_price = float(r.get('price', state['position']['stop']))
                pnl = (state['position']['qty'] * stop_price * (1-FEE)) - state['position']['cost']
                state['trades'].append({
                    'date': r.get('time', ''),
                    'entry': state['position']['entry'],
                    'exit': stop_price, 'pnl': round(pnl,6),
                    'reason': 'stop_loss_binance'
                })
                state['position'] = None

    # paper stop loss check
    if PAPER_MODE and state['position']:
        if current_price <= state['position']['stop']:
            print(f'Stop loss triggered: ${current_price:,.2f} <= ${state["position"]["stop"]:,.2f}')
            paper_sell(state, current_price, 'stop_loss')

    pos = state['position']

    if signal == 'BUY' and pos is None:
        if PAPER_MODE:
            paper_buy(state, sig_price)
        else:
            live_buy(state, sig_price, df4h)

    elif signal == 'SELL' and pos is not None:
        if PAPER_MODE:
            paper_sell(state, sig_price, 'signal')
        else:
            live_sell(state, 'signal')

    print_summary(state, current_price, signal, yes_count, conditions)
    save_state(state)

if __name__ == '__main__':
    main()
