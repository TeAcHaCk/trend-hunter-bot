"""
Delta Exchange India REST API Client.
Implements HMAC-SHA256 authentication per official docs.
"""
import hmac
import hashlib
import time
import json
import logging
from typing import Optional, Dict, Any

import requests

from backend.config import settings

logger = logging.getLogger(__name__)


class DeltaClient:
    """REST API wrapper for Delta Exchange India."""

    def __init__(self, api_key: str = "", api_secret: str = ""):
        self.api_key = api_key or settings.DELTA_API_KEY
        self.api_secret = api_secret or settings.DELTA_API_SECRET
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "python-rest-client",
            "Content-Type": "application/json",
        })
        logger.info(f"DeltaClient initialized | key={'***'+self.api_key[-4:] if len(self.api_key)>4 else '(empty)'} | url={self.base_url}")

    @property
    def base_url(self) -> str:
        return settings.rest_url

    def _generate_signature(self, method: str, path: str,
                            query_string: str = "", payload: str = "") -> Dict[str, str]:
        """
        Generate HMAC-SHA256 signature per Delta Exchange docs.
        Signature = HMAC-SHA256(secret, method + timestamp + path + query_string + payload)
        """
        timestamp = str(int(time.time()))
        signature_data = method + timestamp + path + query_string + payload

        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_data.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        logger.debug(
            f"Signature generated | method={method} | path={path} | "
            f"ts={timestamp} | qs={query_string[:50] if query_string else '(none)'} | "
            f"payload={payload[:80] if payload else '(none)'} | sig={signature[:16]}..."
        )

        return {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
        }

    def _request(self, method: str, path: str,
                 params: Optional[Dict] = None,
                 data: Optional[Dict] = None,
                 auth: bool = False,
                 max_retries: int = 3) -> Dict[str, Any]:
        """Make an API request with optional authentication and retry logic."""
        url = f"{self.base_url}{path}"
        query_string = ""
        payload = ""

        if params:
            query_string = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        if data:
            payload = json.dumps(data, separators=(",", ":"))

        headers = {}
        if auth:
            if not self.api_key or not self.api_secret:
                logger.error("API key or secret is empty! Cannot authenticate.")
                return {"success": False, "error": {"code": "missing_credentials", "message": "API key or secret not configured"}}
            headers = self._generate_signature(method, path, query_string, payload)

        for attempt in range(max_retries):
            try:
                if auth:
                    # Regenerate signature on retry (timestamp freshness)
                    headers = self._generate_signature(method, path, query_string, payload)

                response = self._session.request(
                    method=method,
                    url=url + (query_string if params else ""),
                    data=payload if data else None,
                    headers=headers,
                    timeout=(5, 30),
                )

                if response.status_code == 429:
                    reset_ms = int(response.headers.get("X-RATE-LIMIT-RESET", 1000))
                    wait_time = max(reset_ms / 1000, 1)
                    logger.warning(f"Rate limited. Waiting {wait_time}s before retry...")
                    time.sleep(wait_time)
                    continue

                # Log auth failures with response body for debugging
                if response.status_code == 401:
                    try:
                        error_body = response.json()
                    except Exception:
                        error_body = response.text[:200]
                    logger.error(
                        f"AUTH FAILED (401) | {method} {path} | "
                        f"Response: {error_body} | "
                        f"Key: {'***'+self.api_key[-4:] if len(self.api_key)>4 else '(empty)'} | "
                        f"URL: {url}"
                    )
                    return {"success": False, "error": error_body.get("error", error_body) if isinstance(error_body, dict) else {"code": "unauthorized", "message": str(error_body)}}

                if response.status_code == 403:
                    try:
                        error_body = response.json()
                    except Exception:
                        error_body = response.text[:200]
                    logger.error(f"FORBIDDEN (403) | {method} {path} | Response: {error_body}")
                    return {"success": False, "error": {"code": "forbidden", "message": str(error_body)}}

                response.raise_for_status()
                return response.json()

            except requests.exceptions.RequestException as e:
                logger.error(f"Request failed (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    return {"success": False, "error": {"code": "request_failed", "message": str(e)}}

        return {"success": False, "error": {"code": "max_retries", "message": "Max retries exceeded"}}

    # ─── Public Endpoints ──────────────────────

    def get_products(self) -> Dict:
        """Get list of all trading products."""
        return self._request("GET", "/v2/products")

    def get_product_by_symbol(self, symbol: str) -> Dict:
        """Get product details by symbol."""
        return self._request("GET", f"/v2/products/{symbol}")

    def get_ticker(self, symbol: str) -> Dict:
        """Get live ticker data including 24h high/low."""
        return self._request("GET", f"/v2/tickers/{symbol}")

    def get_tickers(self, contract_types: str = "perpetual_futures") -> Dict:
        """Get tickers for specified contract types."""
        return self._request("GET", "/v2/tickers", params={"contract_types": contract_types})

    def get_orderbook(self, symbol: str) -> Dict:
        """Get L2 orderbook."""
        return self._request("GET", f"/v2/l2orderbook/{symbol}")

    def get_candles(self, symbol: str, resolution: str = "15m",
                    num_candles: int = 6) -> list:
        """
        Fetch recent OHLC candles from Delta Exchange.

        Args:
            symbol: Trading pair (e.g., "BTCUSD", "ETHUSD")
            resolution: Candle timeframe - "1m", "5m", "15m", "1h", "1d"
            num_candles: Number of candles to fetch (e.g., 6 = 1.5hrs on 15m)

        Returns:
            List of candle dicts with: open, high, low, close, volume, time
            Sorted newest first.
        """
        resolution_seconds = {
            "1m": 60, "5m": 300, "15m": 900,
            "30m": 1800, "1h": 3600, "1d": 86400,
        }
        secs = resolution_seconds.get(resolution, 900)
        now = int(time.time())
        start = now - (num_candles * secs) - secs  # Extra buffer for partial candle

        result = self._request("GET", "/v2/history/candles", params={
            "resolution": resolution,
            "symbol": symbol,
            "start": str(start),
            "end": str(now),
        })

        candles = result.get("result", [])
        if candles:
            logger.debug(f"Fetched {len(candles)} candles for {symbol} ({resolution})")
        else:
            logger.warning(f"No candle data returned for {symbol} ({resolution})")
        return candles

    # ─── Authenticated Endpoints ──────────────────────

    def test_connection(self) -> Dict:
        """Test API connection by fetching wallet balances."""
        return self.get_wallet_balances()

    def get_wallet_balances(self) -> Dict:
        """Get wallet balances."""
        return self._request("GET", "/v2/wallet/balances", auth=True)

    def get_positions(self) -> Dict:
        """Get all margined positions."""
        return self._request("GET", "/v2/positions/margined", auth=True)

    def get_position(self, product_id: int) -> Dict:
        """Get position for a specific product."""
        return self._request("GET", "/v2/positions", params={"product_id": product_id}, auth=True)

    def get_active_orders(self, product_id: Optional[int] = None) -> Dict:
        """Get active orders, optionally filtered by product."""
        params = {}
        if product_id:
            params["product_id"] = product_id
        return self._request("GET", "/v2/orders", params=params if params else None, auth=True)

    def get_order_history(self, page_size: int = 50) -> Dict:
        """Get order history (cancelled and closed)."""
        return self._request("GET", "/v2/orders/history",
                             params={"page_size": page_size}, auth=True)

    def place_order(self, product_id: int, product_symbol: str,
                    side: str, size: int,
                    order_type: str = "market_order",
                    limit_price: Optional[str] = None,
                    reduce_only: bool = False) -> Dict:
        """
        Place a new order on Delta Exchange.

        Args:
            product_id: Product ID (e.g., 27 for BTCUSD)
            product_symbol: Symbol (e.g., "BTCUSD")
            side: "buy" or "sell"
            size: Number of contracts
            order_type: "market_order" or "limit_order"
            limit_price: Required for limit orders
            reduce_only: If True, only reduces existing position
        """
        payload = {
            "product_id": product_id,
            "product_symbol": product_symbol,
            "size": size,
            "side": side,
            "order_type": order_type,
        }

        if order_type == "limit_order" and limit_price:
            payload["limit_price"] = limit_price

        if reduce_only:
            payload["reduce_only"] = True

        logger.info(f"Placing order: {side} {size} {product_symbol} ({order_type})")
        return self._request("POST", "/v2/orders", data=payload, auth=True)

    def cancel_order(self, order_id: int, product_id: int) -> Dict:
        """Cancel an existing order."""
        payload = {"id": order_id, "product_id": product_id}
        return self._request("DELETE", "/v2/orders", data=payload, auth=True)

    def cancel_all_orders(self, product_id: Optional[int] = None) -> Dict:
        """Cancel all open orders, optionally for a specific product."""
        payload = {}
        if product_id:
            payload["product_id"] = product_id
        return self._request("DELETE", "/v2/orders/all", data=payload, auth=True)

    def close_position(self, product_id: int, product_symbol: str,
                       current_side: str, size: int) -> Dict:
        """
        Close an existing position by placing an opposite market order.

        Args:
            current_side: "LONG" or "SHORT" (the current position direction)
            size: Number of contracts to close
        """
        close_side = "sell" if current_side == "LONG" else "buy"
        return self.place_order(
            product_id=product_id,
            product_symbol=product_symbol,
            side=close_side,
            size=size,
            order_type="market_order",
            reduce_only=True,
        )

    def place_bracket_order(self, product_id: int,
                            stop_loss_price: float,
                            take_profit_price: float,
                            trail_amount: Optional[float] = None) -> Dict:
        """
        Place a bracket order (SL + TP) on an existing position.
        Uses OCO logic — when one triggers, the other cancels.

        Args:
            product_id: Product ID
            stop_loss_price: Mark price to trigger stop loss
            take_profit_price: Mark price to trigger take profit
        """
        payload = {
            "product_id": product_id,
            "bracket_stop_loss_price": str(stop_loss_price),
            "bracket_take_profit_price": str(take_profit_price),
        }
        if trail_amount:
            payload["bracket_trail_amount"] = str(trail_amount)

        logger.info(
            f"Placing bracket order | PID={product_id} | "
            f"SL={stop_loss_price} | TP={take_profit_price}"
        )
        return self._request("POST", "/v2/orders/bracket", data=payload, auth=True)

    def get_order_by_id(self, order_id: int) -> Dict:
        """Get order details by ID."""
        return self._request("GET", f"/v2/orders/{order_id}", auth=True)

    def cancel_product_orders(self, product_id: int) -> Dict:
        """Cancel all open orders for a specific product."""
        return self.cancel_all_orders(product_id=product_id)

    def set_leverage(self, product_id: int, leverage: int) -> Dict:
        """Set leverage for a product."""
        payload = {
            "product_id": product_id,
            "leverage": str(leverage),
        }
        return self._request("POST", "/v2/orders/leverage", data=payload, auth=True)

    # ─── Utility Methods ──────────────────────

    def get_product_id(self, symbol: str) -> Optional[int]:
        """Resolve a symbol to its product ID."""
        result = self.get_product_by_symbol(symbol)
        if result.get("success") and result.get("result"):
            return result["result"]["id"]
        return None

    def get_24h_high_low(self, symbol: str) -> Optional[Dict[str, float]]:
        """Get the 24h high and low for a symbol from ticker data."""
        result = self.get_ticker(symbol)
        if result.get("success") and result.get("result"):
            ticker = result["result"]
            return {
                "high": float(ticker.get("high", 0)),
                "low": float(ticker.get("low", 0)),
                "close": float(ticker.get("close", 0)),
                "mark_price": float(ticker.get("mark_price", 0)),
                "spot_price": float(ticker.get("spot_price", 0)),
                "volume": float(ticker.get("volume", 0)),
            }
        return None
