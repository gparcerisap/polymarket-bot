"""
polymarket_bot.py
=================
Bot de trading para Polymarket — mercados de predicción descentralizados.
Opera en TODOS los mercados activos comprando tokens YES/NO.

Estructura basada en trading_bot.py (el que ganó dinero).

Estrategia:
  - Ciclos cada 2 minutos (óptimo para Polymarket)
  - Análisis usando datos del mercado directamente (sin llamadas extra)
  - Scoring de riesgo por mercado (HIGH/MEDIUM/LOW)
  - Stop-loss: -20% | Take-profit: +40%
  - Máx $30 USD en HIGH risk | $75 MEDIUM | $150 LOW
  - Budget total: $500 USD
"""

import os
import sys
import json
import time
import uuid
import logging
import traceback
import requests
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional
from pathlib import Path

# ─── Agregar py_clob_client al path ──────────────────────────────────────────
CLOB_LIB = Path(__file__).parent.parent / "py-clob-client-main 2"
if CLOB_LIB.exists():
    sys.path.insert(0, str(CLOB_LIB))

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"poly_trading_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(log_file)),
    ],
)
log = logging.getLogger(__name__)

# ─── Parámetros globales ──────────────────────────────────────────────────────
MAX_BUDGET_USD      = 1_600.0   # Todo el capital disponible
CYCLE_SECONDS       = 120       # 2 minutos — óptimo para Polymarket
STOP_LOSS_PCT       = 0.20      # -20%
TAKE_PROFIT_PCT     = 0.40      # +40%
MIN_ORDER_USD       = 5.0
MAX_OPEN_POSITIONS  = 15        # Más posiciones para distribuir el capital

RISK_LIMITS = {
    "HIGH":   30.0,    # Máx $30 en mercados arriesgados
    "MEDIUM": 100.0,   # Máx $100 en mercados medios
    "LOW":    200.0,   # Máx $200 en mercados seguros
}

# Filtros de mercado
MIN_VOLUME_24H  = 1_000.0
MIN_LIQUIDITY   = 500.0
MIN_HOURS_LEFT  = 24
MIN_PRICE       = 0.05
MAX_PRICE       = 0.95
MAX_SPREAD      = 0.08    # Spread máximo aceptable (8%) → liquidez mínima

# Polymarket endpoints
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID  = 137

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")

# ─── Proxy residencial (para bypass geoblock) ─────────────────────────────────
# Usar variable custom POLY_PROXY para no interferir con el build de Railway
PROXY_URL = os.environ.get("POLY_PROXY", "")
if PROXY_URL:
    # Solo aplicar en runtime del bot, no durante el build
    os.environ["HTTPS_PROXY"] = PROXY_URL
    os.environ["HTTP_PROXY"]  = PROXY_URL
    os.environ["https_proxy"] = PROXY_URL
    os.environ["http_proxy"]  = PROXY_URL
    log_proxy = PROXY_URL.split("@")[-1] if "@" in PROXY_URL else PROXY_URL
    # log se configura después, guardar para mostrar luego

# ─── Cargar .env ──────────────────────────────────────────────────────────────
def load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file, override=True)
        except ImportError:
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ[k.strip()] = v.strip().strip('"').strip("'")

load_env()

# ─── Modelo de posición ───────────────────────────────────────────────────────
@dataclass
class Position:
    condition_id: str
    token_id:     str
    outcome:      str
    question:     str
    qty:          float
    entry_price:  float
    entry_usd:    float
    order_id:     str
    risk_level:   str
    opened_at:    str

    def pnl_pct(self, current_price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        return (current_price - self.entry_price) / self.entry_price

    def should_stop_loss(self, p: float) -> bool:
        return self.pnl_pct(p) <= -STOP_LOSS_PCT

    def should_take_profit(self, p: float) -> bool:
        return self.pnl_pct(p) >= TAKE_PROFIT_PCT


# ─── Helpers ──────────────────────────────────────────────────────────────────
def parse_json_field(val) -> list:
    """Parsea un campo que puede venir como JSON string o como lista."""
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return []
    return []


def get_token_ids(market: dict) -> list:
    """
    Extrae los token_ids y outcomes del mercado.
    clobTokenIds viene como JSON string: '["token1", "token2"]'
    outcomes viene como JSON string: '["Yes", "No"]'
    """
    clob_ids = parse_json_field(market.get("clobTokenIds", []))
    outcomes = parse_json_field(market.get("outcomes", []))

    result = []
    for i, tid in enumerate(clob_ids):
        if isinstance(tid, str) and len(tid) > 10:
            outcome = outcomes[i] if i < len(outcomes) else ("YES" if i == 0 else "NO")
            result.append({"token_id": tid, "outcome": outcome})
    return result


# ─── Gamma API client ─────────────────────────────────────────────────────────
class GammaClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        if PROXY_URL:
            self.session.proxies = {"http": PROXY_URL, "https": PROXY_URL}

    def get_active_markets(self, limit: int = 200) -> list:
        markets = []
        offset  = 0
        while True:
            try:
                r = self.session.get(
                    f"{GAMMA_API}/markets",
                    params={"active": "true", "closed": "false",
                            "limit": min(limit, 100), "offset": offset},
                    timeout=15,
                )
                if r.status_code != 200:
                    break
                batch = r.json()
                if not batch:
                    break
                markets.extend(batch)
                if len(batch) < 100 or len(markets) >= limit:
                    break
                offset += 100
                time.sleep(0.2)
            except Exception as e:
                log.warning(f"get_active_markets: {e}")
                break
        return markets

    def get_market_by_condition(self, condition_id: str) -> Optional[dict]:
        """Obtiene datos frescos de un mercado por conditionId."""
        try:
            r = self.session.get(
                f"{GAMMA_API}/markets",
                params={"conditionId": condition_id},
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    return data[0]
                if isinstance(data, dict):
                    return data
        except Exception as e:
            log.debug(f"get_market_by_condition: {e}")
        return None


# ─── CLOB client (solo para órdenes) ─────────────────────────────────────────
class PolyClient:
    def __init__(self):
        self._clob = None
        self._init_clob_client()

    def _init_clob_client(self):
        private_key = os.environ.get("POLY_PRIVATE_KEY", "")
        api_key     = os.environ.get("POLY_API_KEY", "")
        api_secret  = os.environ.get("POLY_API_SECRET", "")
        api_pass    = os.environ.get("POLY_API_PASSPHRASE", "")

        if not private_key:
            log.warning("POLY_PRIVATE_KEY no configurada — solo análisis")
            return

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            if api_key and api_secret and api_pass:
                creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass)
                self._clob = ClobClient(
                    host=CLOB_HOST,
                    chain_id=int(os.environ.get("POLY_CHAIN_ID", CHAIN_ID)),
                    key=private_key,
                    creds=creds,
                )
                log.info("✅ ClobClient L2 listo (trading completo)")
            else:
                self._clob = ClobClient(
                    host=CLOB_HOST,
                    chain_id=int(os.environ.get("POLY_CHAIN_ID", CHAIN_ID)),
                    key=private_key,
                )
                log.info("ClobClient L1 — se necesitan API creds para tradear")
        except Exception as e:
            log.warning(f"Error iniciando ClobClient: {e}")

    def market_buy(self, token_id: str, usd: float, price: float) -> Optional[dict]:
        if DRY_RUN:
            log.info(f"  [DRY-RUN] COMPRA {token_id[:14]}… ${usd:.2f} @ {price:.3f}")
            return {"order_id": f"dry-{uuid.uuid4().hex[:8]}", "size": str(round(usd / price, 4))}

        if not self._clob:
            log.error("ClobClient no disponible")
            return None
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            qty = round(usd / price, 4) if price > 0 else 0
            if qty <= 0:
                return None
            args  = MarketOrderArgs(token_id=token_id, amount=qty, side="BUY")
            order = self._clob.create_market_order(args)
            if order:
                return self._clob.post_order(order, OrderType.FOK)
        except Exception as e:
            log.error(f"market_buy {token_id[:14]}: {e}")
            log.debug(traceback.format_exc())
        return None

    def market_sell(self, token_id: str, qty: float, price: float) -> Optional[dict]:
        if DRY_RUN:
            log.info(f"  [DRY-RUN] VENTA {token_id[:14]}… {qty:.4f} @ {price:.3f}")
            return {"order_id": f"dry-{uuid.uuid4().hex[:8]}", "filled": str(qty)}

        if not self._clob:
            log.error("ClobClient no disponible")
            return None
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            args  = MarketOrderArgs(token_id=token_id, amount=qty, side="SELL")
            order = self._clob.create_market_order(args)
            if order:
                return self._clob.post_order(order, OrderType.FOK)
        except Exception as e:
            log.error(f"market_sell {token_id[:14]}: {e}")
            log.debug(traceback.format_exc())
        return None

    def can_trade(self) -> bool:
        return DRY_RUN or (self._clob is not None)


# ─── Análisis de mercado (usa datos Gamma directamente) ───────────────────────
def score_risk_fast(market: dict) -> str:
    """
    Clasifica riesgo solo con datos ya disponibles en el mercado.
    Sin llamadas extra a CLOB.
    """
    score = 0

    vol = float(market.get("volume24hr") or 0)
    if vol < 5_000:
        score += 2
    elif vol < 50_000:
        score += 1

    liq = float(market.get("liquidityNum") or 0)
    if liq < 1_000:
        score += 2
    elif liq < 10_000:
        score += 1

    spread = float(market.get("spread") or 1)
    if spread > 0.05:
        score += 2
    elif spread > 0.02:
        score += 1

    end_str = market.get("endDateIso") or market.get("endDate") or ""
    if end_str:
        try:
            end_dt    = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_left < 24:
                score += 3
            elif hours_left < 168:
                score += 1
        except Exception:
            pass

    if score >= 4:
        return "HIGH"
    elif score >= 2:
        return "MEDIUM"
    return "LOW"


def analyze_market(market: dict) -> Optional[dict]:
    """
    Analiza el mercado usando SOLO datos de la Gamma API.
    Cero llamadas al CLOB — rápido y sin errores 404.

    Señales:
    - Value bet: token cotiza < 45¢ con spread bajo → probable subida
    - Momentum: precio subiendo respecto al last_trade
    - Descuento: precio actual < precio histórico reciente
    """
    condition_id = market.get("conditionId", "")
    question     = market.get("question", "")[:80]
    if not condition_id:
        return None

    tokens = get_token_ids(market)
    if len(tokens) < 2:
        return None

    prices_raw = parse_json_field(market.get("outcomePrices", []))
    if len(prices_raw) < 2:
        return None

    try:
        yes_price = float(prices_raw[0])
        no_price  = float(prices_raw[1])
    except Exception:
        return None

    spread     = float(market.get("spread") or 1)
    last_trade = float(market.get("lastTradePrice") or yes_price)
    best_bid   = float(market.get("bestBid") or 0)
    best_ask   = float(market.get("bestAsk") or 1)
    vol_24h    = float(market.get("volume24hr") or 0)

    # Descartar si spread alto (poco líquido) o precios extremos
    if spread > MAX_SPREAD:
        return None

    risk = score_risk_fast(market)

    best_signal = None
    best_score  = 0.0

    for i, token_info in enumerate(tokens[:2]):
        token_id = token_info["token_id"]
        outcome  = token_info["outcome"]
        price    = yes_price if i == 0 else no_price

        if price < MIN_PRICE or price > MAX_PRICE:
            continue

        score = 0.0

        # 1. Value bet: precio bajo → potencial de subida
        if price < 0.40:
            score += (0.40 - price) * 3          # máx ~1.2

        # 2. Momentum positivo (último trade empujando hacia arriba)
        if i == 0:
            momentum = yes_price - last_trade
        else:
            momentum = no_price - (1 - last_trade)

        if momentum > 0.005:
            score += momentum * 5

        # 3. Buen bid/ask (spread estrecho = señal de confianza)
        effective_spread = best_ask - best_bid if best_ask > best_bid else spread
        if effective_spread < 0.02:
            score += 0.2
        elif effective_spread < 0.04:
            score += 0.1

        # 4. Volumen alto = mercado activo = señal más confiable
        if vol_24h > 100_000:
            score += 0.3
        elif vol_24h > 20_000:
            score += 0.15

        # Solo señal BUY si hay score mínimo
        if score > 0.15 and score > best_score:
            best_score  = score
            best_signal = {
                "condition_id": condition_id,
                "token_id":     token_id,
                "outcome":      outcome,
                "question":     question,
                "price":        price,
                "signal":       "BUY",
                "risk_level":   risk,
                "score":        round(score, 3),
                "spread":       round(spread, 4),
                "momentum":     round(momentum, 4),
                "obi":          0.0,
            }

    return best_signal


# ─── Motor de trading ─────────────────────────────────────────────────────────
class PolymarketBot:
    def __init__(self):
        self.poly      = PolyClient()
        self.gamma     = GammaClient()
        self.positions: dict[str, Position] = {}
        self.total_pnl = 0.0
        self.trades    = 0
        self.cycle_n   = 0
        self._load_positions()

    def _load_positions(self):
        pos_file = LOG_DIR / "poly_positions.json"
        if pos_file.exists():
            try:
                data = json.loads(pos_file.read_text())
                for cid, p in data.items():
                    self.positions[cid] = Position(**p)
                if self.positions:
                    log.info(f"Posiciones previas cargadas: {len(self.positions)}")
            except Exception:
                pass

    def _save_state(self):
        (LOG_DIR / "poly_positions.json").write_text(
            json.dumps({cid: asdict(p) for cid, p in self.positions.items()}, indent=2)
        )
        (LOG_DIR / "poly_stats.json").write_text(json.dumps({
            "total_pnl":      round(self.total_pnl, 4),
            "trades":         self.trades,
            "cycles":         self.cycle_n,
            "open_positions": len(self.positions),
            "updated_at":     datetime.now(timezone.utc).isoformat(),
            "dry_run":        DRY_RUN,
        }, indent=2))

    def invested_usd(self) -> float:
        return sum(p.entry_usd for p in self.positions.values())

    def available_capital(self) -> float:
        return max(0.0, MAX_BUDGET_USD - self.invested_usd())

    def get_current_price(self, pos: Position) -> float:
        """
        Precio actual de una posición via Gamma API (sin llamadas CLOB).
        """
        market = self.gamma.get_market_by_condition(pos.condition_id)
        if not market:
            return 0.0
        try:
            prices_raw = parse_json_field(market.get("outcomePrices", []))
            tokens     = get_token_ids(market)
            for i, t in enumerate(tokens):
                if t["token_id"] == pos.token_id and i < len(prices_raw):
                    return float(prices_raw[i])
        except Exception:
            pass
        return 0.0

    # ── Ciclo principal ───────────────────────────────────────────────────────
    def run_cycle(self):
        self.cycle_n += 1
        ts = datetime.now().strftime("%H:%M:%S")
        log.info(f"\n{'─'*64}")
        log.info(f"⚡ Ciclo #{self.cycle_n} — {ts} | PnL: ${self.total_pnl:+.2f} | Posiciones: {len(self.positions)}")
        log.info(f"{'─'*64}")

        # 1. Revisar posiciones abiertas (precio via Gamma)
        for cid in list(self.positions.keys()):
            pos   = self.positions[cid]
            price = self.get_current_price(pos)
            if price <= 0:
                continue

            pnl_pct = pos.pnl_pct(price)
            pnl_usd = pos.entry_usd * pnl_pct
            log.info(
                f"  📌 [{pos.risk_level}] {pos.outcome} — {pos.question[:48]}… "
                f"entrada={pos.entry_price:.3f} actual={price:.3f} "
                f"PnL={pnl_pct*100:+.1f}% (${pnl_usd:+.2f})"
            )

            reason = None
            if pos.should_stop_loss(price):
                reason = f"STOP-LOSS ({pnl_pct*100:.1f}%)"
            elif pos.should_take_profit(price):
                reason = f"TAKE-PROFIT ({pnl_pct*100:.1f}%)"

            if reason:
                self._sell(pos, price, reason)

        # 2. Nuevas oportunidades
        capital = self.available_capital()
        if capital < MIN_ORDER_USD:
            log.info(f"  ⏸ Capital insuficiente (${capital:.2f})")
            self._save_state()
            return

        if len(self.positions) >= MAX_OPEN_POSITIONS:
            log.info(f"  ⏸ Máx posiciones ({MAX_OPEN_POSITIONS}) alcanzado")
            self._save_state()
            return

        log.info(f"  🔎 Escaneando mercados… (capital: ${capital:.2f})")
        markets = self.gamma.get_active_markets(limit=200)
        log.info(f"  📊 {len(markets)} mercados obtenidos")

        # Filtrar y analizar
        signals = []
        skipped = 0
        for m in markets:
            vol = float(m.get("volume24hr") or 0)
            liq = float(m.get("liquidityNum") or 0)
            if vol < MIN_VOLUME_24H or liq < MIN_LIQUIDITY:
                skipped += 1
                continue

            end_str = m.get("endDateIso") or m.get("endDate") or ""
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    if (end_dt - datetime.now(timezone.utc)).total_seconds() < MIN_HOURS_LEFT * 3600:
                        skipped += 1
                        continue
                except Exception:
                    pass

            cid = m.get("conditionId", "")
            if cid in self.positions:
                continue

            sig = analyze_market(m)
            if sig and sig["signal"] == "BUY":
                signals.append(sig)

        signals.sort(key=lambda s: s["score"], reverse=True)
        log.info(f"  ✅ {len(signals)} señales BUY | {skipped} descartados por volumen/liquidez")

        # Ejecutar mejores oportunidades
        for sig in signals[:5]:  # máx 5 compras por ciclo
            capital = self.available_capital()
            if capital < MIN_ORDER_USD or len(self.positions) >= MAX_OPEN_POSITIONS:
                break

            risk    = sig["risk_level"]
            max_usd = min(RISK_LIMITS.get(risk, 30.0), capital)
            alloc   = round(max(MIN_ORDER_USD, min(max_usd, capital)), 2)

            log.info(
                f"\n  📈 [{risk}] {sig['outcome']} — {sig['question'][:55]}…\n"
                f"     precio={sig['price']:.3f} | spread={sig['spread']:.4f} "
                f"| score={sig['score']:.3f}"
            )
            self._buy(sig, alloc)

        self._save_state()

    def _buy(self, sig: dict, usd: float):
        log.info(f"  🟢 COMPRANDO {sig['outcome']} — ${usd:.2f} @ {sig['price']:.3f}")
        order = self.poly.market_buy(sig["token_id"], usd, sig["price"])
        if not order:
            log.error(f"  ❌ Compra fallida: {sig['question'][:40]}")
            return

        qty = usd / sig["price"] if sig["price"] > 0 else 0
        try:
            qty = float(order.get("size") or order.get("filled") or order.get("sizeFilled") or qty)
        except Exception:
            pass

        cid = sig["condition_id"]
        self.positions[cid] = Position(
            condition_id=cid,
            token_id=sig["token_id"],
            outcome=sig["outcome"],
            question=sig["question"],
            qty=qty,
            entry_price=sig["price"],
            entry_usd=usd,
            order_id=str(order.get("orderID") or order.get("order_id", "")),
            risk_level=sig["risk_level"],
            opened_at=datetime.now(timezone.utc).isoformat(),
        )
        self.trades += 1
        log.info(f"  ✅ COMPRA OK: {qty:.4f} tokens {sig['outcome']} @ {sig['price']:.3f} — {sig['question'][:45]}…")

    def _sell(self, pos: Position, price: float, reason: str):
        log.info(f"\n  🔴 VENDIENDO {pos.outcome} — {reason}")
        order = self.poly.market_sell(pos.token_id, pos.qty, price)
        if not order:
            log.error(f"  ❌ Venta fallida: {pos.question[:40]}")
            return

        pnl_usd = pos.entry_usd * pos.pnl_pct(price)
        self.total_pnl += pnl_usd
        self.trades    += 1
        del self.positions[pos.condition_id]
        log.info(
            f"  ✅ VENTA OK: {pos.qty:.4f} tokens {pos.outcome} "
            f"@ {price:.3f} | PnL: ${pnl_usd:+.2f} | Total PnL: ${self.total_pnl:+.2f}"
        )

    # ── Loop ──────────────────────────────────────────────────────────────────
    def run(self):
        mode = "DRY-RUN 🟡" if DRY_RUN else "PRODUCCIÓN 🔴"
        log.info("=" * 64)
        log.info(f"🚀 BOT POLYMARKET — {mode}")
        log.info(f"   Budget: ${MAX_BUDGET_USD:.0f} | Ciclo: {CYCLE_SECONDS}s | Posiciones máx: {MAX_OPEN_POSITIONS}")
        log.info(f"   Límites: HIGH=${RISK_LIMITS['HIGH']} | MEDIUM=${RISK_LIMITS['MEDIUM']} | LOW=${RISK_LIMITS['LOW']}")
        log.info(f"   Stop-loss: -{STOP_LOSS_PCT*100:.0f}% | Take-profit: +{TAKE_PROFIT_PCT*100:.0f}%")
        if not self.poly.can_trade():
            log.warning("   ⚠ Sin credenciales — solo análisis")
        log.info("=" * 64)

        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                log.info("\n⛔ Bot detenido.")
                self._save_state()
                break
            except Exception as e:
                log.error(f"Error en ciclo: {e}")
                log.debug(traceback.format_exc())

            try:
                log.info(f"\n  ⏳ Próximo ciclo en {CYCLE_SECONDS}s…")
                time.sleep(CYCLE_SECONDS)
            except KeyboardInterrupt:
                log.info("\n⛔ Bot detenido.")
                self._save_state()
                break


if __name__ == "__main__":
    bot = PolymarketBot()
    bot.run()
