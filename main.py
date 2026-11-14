"""Burst-regime bot for btc-race-24h-20261113-2314.

Own design: a momentum-burst entry gated by a trend regime, with a
trailing-stop exit. Nothing here copies a kit baseline (no buy-and-hold,
no plain EMA cross, no RSI/Bollinger, no 24-bar momentum rebalance).

Idea
----
cbBTC on this pool moves in short, sharp bursts inside a slow drift.
A 0.4% rise over 5 minutes is a ~2.5-sigma event, so it marks a real
buying impulse rather than noise. But bursts against the prevailing
trend mostly reverse, so the entry is allowed only while the fast EMA
(12) sits above the slow EMA (48): the regime filter. Once in, the
position is protected by a 0.35% trailing stop off the highest close
since entry, a 0.2% hard stop off the fill price, and a 60-bar timeout,
so losers are cut fast and the rare real rally is ridden to its end.
Entries use the full cash balance: the round trip costs ~30 bps, so a
trade only pays when it catches a move well above that, and sizing
down would only shrink the payoff without changing the per-trade edge.

Protocol: agent-fun/bars-replay/v1 via the vendored SDK shim.
Deterministic: pure function of the closes seen so far; no randomness,
no wall clock, no network, no files.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- tuned on the dev window (1,440 bars) with a local replay sim ---
BURST_WIN = 5          # minutes over which the burst is measured
BURST_THR = 0.004      # +0.4% in BURST_WIN minutes
EMA_FAST = 12
EMA_SLOW = 48
MIN_HISTORY = 60       # bars of closes before the first entry
TRAIL = 0.0035         # trailing stop: 0.35% below the highest close
HARD_STOP = 0.003      # hard stop: 0.3% below the fill price
# NOTE: stops are referenced to the real fill price (open + 2 bps
# slippage + ~7-15 bps impact), which sits ~0.15% above the open.
# A 0.2% hard stop proved too tight against that reference: it
# stopped the first trade out one bar after entry on mere noise.
# 0.3% clears the fill-price offset while still cutting losers fast.
MAX_HOLD = 60          # bars; then exit whatever the price
MAX_SLIPPAGE_BPS = 150  # 1.5%: far above any fill-vs-close gap seen


class Bot:
    def __init__(self):
        self.closes = []
        self.ema_fast = None
        self.ema_slow = None
        self.entry_price = None   # fill price of the open position
        self.entry_high = None    # highest close since entry
        self.entry_bar = None     # seq of the fill batch
        self.pending_buy = None   # decision close, until the fill arrives

    def _ema_step(self, close):
        if self.ema_fast is None:
            self.ema_fast = float(close)
            self.ema_slow = float(close)
        else:
            kf = 2.0 / (EMA_FAST + 1)
            ks = 2.0 / (EMA_SLOW + 1)
            self.ema_fast += kf * (close - self.ema_fast)
            self.ema_slow += ks * (close - self.ema_slow)

    def on_batch(self, state, batch):
        # Fold in any fill of the previous batch's buy so the entry
        # price is the real fill price, not the decision close.
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL:
                continue
            if fill.get("side") == "buy" and fill.get("status") == "filled":
                price = int(fill["price"])
                self.entry_price = price
                self.entry_high = price
                self.entry_bar = state.seq
                self.pending_buy = None
            elif fill.get("side") == "buy":
                # rejected: the pending entry never happened
                self.pending_buy = None

        if state.warming_up:
            return []

        close = state.close(SYMBOL)
        if close is None:
            return []
        self.closes.append(close)
        self._ema_step(close)

        holding = state.positions.get(SYMBOL)
        qty = holding.qty if holding is not None else 0

        if qty <= 0:
            if self.pending_buy is not None:
                # a buy is already in flight; wait for its fill
                return []
            if len(self.closes) < MIN_HISTORY:
                return []
            if len(self.closes) <= BURST_WIN:
                return []
            burst = close / self.closes[-BURST_WIN - 1] - 1.0
            regime_up = self.ema_fast > self.ema_slow
            if burst >= BURST_THR and regime_up and state.cash > 0:
                self.pending_buy = close
                return [{
                    "side": "buy",
                    "symbol": SYMBOL,
                    "notional_quote_units": state.cash,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
            return []

        # In a position: manage the exit.
        if self.entry_price is None:
            # Position exists but no fill was seen (e.g. warm-up edge):
            # fall back to the first close seen while holding.
            self.entry_price = close
            self.entry_high = close
            self.entry_bar = state.seq
        if close > self.entry_high:
            self.entry_high = close
        exit_now = False
        if close <= self.entry_high * (1.0 - TRAIL):
            exit_now = True
        elif close <= self.entry_price * (1.0 - HARD_STOP):
            exit_now = True
        elif state.seq - self.entry_bar >= MAX_HOLD:
            exit_now = True
        if exit_now:
            self.entry_price = None
            self.entry_high = None
            self.entry_bar = None
            return [{
                "side": "sell",
                "symbol": SYMBOL,
                "qty": qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]
        return []


BOT = Bot()
makefun_bars.run(BOT.on_batch)
