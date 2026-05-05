"""
Delta Exchange India REST API Client (async).

Highlights vs the previous sync client:
  * aiohttp + connection pooling — no more event-loop blocking on order calls.
  * Idempotency: callers may pass `client_order_id` to make order placement
    safe to retry without duplicating fills.
  * Centralised retry/backoff with proper handling of 429 / 5xx / network errors.
  * Request timing instrumentation (latency_ms in result envelope) for tuning.
"""
import hmac
import hashlib
import time
import json
import asyncio
import logging
from typing import Optional, Dict, Any, List

import aiohttp

from backend.config import settings

logger = logging.getLogger(__name__)


class DeltaClient:
    """Async REST API wrapper for Delta Exchange India."""

    DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)

    def __init__(self, api_key: str = "", api_secret: str = ""):
        self.api_key = api_key or settings.DELTA_API_KEY
        self.api_secret = api_secret or settings.DELTA_API_SECRET
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        logger.info(
            f"DeltaClient initialized | key={'***'+self.api_key[-4:] if len(self.api_key)>4 else '(empty)'} | url={self.base_url}"
        )

    @property
    def base_url(self) -> str:
        return settings.rest_url

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazily create and reuse a single ClientSession (per event loop)."""
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    connector = aiohttp.TCPConnector(
                        limit=20,
                        ttl_dns_cache=300,
                        keepalive_timeout=60,
                        enable_cleanup_closed=True,
                    )
                    self._session = aiohttp.ClientSession(
                        connector=connector,
                        timeout=self.DEFAULT_TIMEOUT,
                        headers={
                            "User-Agent": "trend-hunter-bot/1.1",
                            "Content-Type": "application/json",
                        },
                    )
        return self._session

    async def close(self):
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _generate_signature(self, method: str, path: str,
                            query_string: str = "", payload: str = "") -> Dict[str, str]:
        """HMAC-SHA256 signature per Delta India docs."""
        timestamp = str(int(time.time()))
        signature_data = method + timestamp + path + query_string + payload
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_data.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
        }

    async def _request(self, method: str, path: str,
                       params: Optional[Dict] = None,
                       data: Optional[Dict] = None,
                       auth: bool = False,
                       max_retries: int = 3) -> Dict[str, Any]:
        """Issue an authenticated/public request with retry + backoff."""
        url = f"{self.base_url}{path}"
        query_string = ""
        payload = ""

        if params:
            query_string = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        if data is not None:
            payload = json.dumps(data, separators=(",", ":"))

        if auth and (not self.api_key or not self.api_secret):
            return {
                "success": False,
                "error": {"code": "missing_credentials",
                          "message": "API key or secret not configured"},
            }

        session = await self._get_session()
        last_error: Optional[Dict] = None

        for attempt in range(max_retries):
            t0 = time.perf_counter()
            try:
                headers = {}
                if auth:
                    headers = self._generate_signature(method, path, query_string, payload)

                request_url = url + query_string
                async with session.request(
                    method=method,
                    url=request_url,
                    data=payload if payload else None,
                    headers=headers,
                ) as response:
                    elapsed_ms = int((time.perf_counter() - t0) * 1000)

                    if response.status == 429:
                        reset_ms = int(response.headers.get("X-RATE-LIMIT-RESET", "1000"))
                        wait_s = max(reset_ms / 1000.0, 1.0)
                        logger.warning(f"Rate limited ({path}); sleeping {wait_s:.2f}s")
                        await asyncio.sleep(wait_s)
                        continue

                    text = await response.text()
                    try:
                        body = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        body = {"raw": text[:500]}

                    if response.status == 401:
                        logger.error(f"AUTH FAILED (401) | {method} {path} | {body}")
                        err = body.get("error", body) if isinstance(body, dict) else body
                        return {"success": False,
                                "error": err if isinstance(err, dict) else {"code": "unauthorized", "message": str(err)},
                                "latency_ms": elapsed_ms}

                    if response.status == 403:
                        logger.error(f"FORBIDDEN (403) | {method} {path} | {body}")
                        return {"success": False,
                                "error": {"code": "forbidden", "message": str(body)},
                                "latency_ms": elapsed_ms}

                    if 500 <= response.status < 600:
                        last_error = {"code": f"http_{response.status}", "message": str(body)[:300]}
                        logger.warning(f"5xx from {path}: {last_error}")
                        await asyncio.sleep(min(2 ** attempt, 5))
                        continue

                    if response.status >= 400:
                        return {"success": False,
                                "error": body.get("error", body) if isinstance(body, dict) else body,
                                "latency_ms": elapsed_ms}

                    if isinstance(body, dict):
                        body.setdefault("latency_ms", elapsed_ms)
                    return body

            except asyncio.TimeoutError:
                last_error = {"code": "timeout", "message": f"timeout after {self.DEFAULT_TIMEOUT.total}s"}
                logger.warning(f"Timeout {method} {path} (attempt {attempt+1}/{max_retries})")
                await asyncio.sleep(min(2 ** attempt, 5))
            except aiohttp.ClientError as e:
                last_error = {"code": "request_failed", "message": str(e)}
                logger.warning(f"ClientError {method} {path}: {e}")
                await asyncio.sleep(min(2 ** attempt, 5))

        return {"success": False,
                "error": last_error or {"code": "max_retries", "message": "Max retries exceeded"}}

    # ─── Public Endpoints ──────────────────────

    async def get_products(self) -> Dict:
        return await self._request("GET", "/v2/products")

    async def get_product_by_symbol(self, symbol: str) -> Dict:
        return await self._request("GET", f"/v2/products/{symbol}")

    async def get_ticker(self, symbol: str) -> Dict:
        return await self._request("GET", f"/v2/tickers/{symbol}")

    async def get_tickers(self, contract_types: str = "perpetual_futures") -> Dict:
        return await self._request("GET", "/v2/tickers", params={"contract_types": contract_types})

    async def get_orderbook(self, symbol: str) -> Dict:
        return await self._request("GET", f"/v2/l2orderbook/{symbol}")

    async def get_candles(self, symbol: str, resolution: str = "15m",
                          num_candles: int = 6) -> List[Dict]:
        resolution_seconds = {
            "1m": 60, "5m": 300, "15m": 900,
            "30m": 1800, "1h": 3600, "1d": 86400,
        }
        secs = resolution_seconds.get(resolution, 900)
        now = int(time.time())
        start = now - (num_candles * secs) - secs

        result = await self._request("GET", "/v2/history/candles", params={
            "resolution": resolution,
            "symbol": symbol,
            "start": str(start),
            "end": str(now),
        })
        candles = result.get("result", []) if isinstance(result, dict) else []
        if not candles:
            logger.warning(f"No candle data returned for {symbol} ({resolution})")
        return candles

    # ─── Authenticated Endpoints ──────────────────────

    async def test_connection(self) -> Dict:
        return await self.get_wallet_balances()

    async def get_wallet_balances(self) -> Dict:
        return await self._request("GET", "/v2/wallet/balances", auth=True)

    async def get_positions(self) -> Dict:
        return await self._request("GET", "/v2/positions/margined", auth=True)

    async def get_position(self, product_id: int) -> Dict:
        return await self._request("GET", "/v2/positions",
                                   params={"product_id": product_id}, auth=True)

    async def get_active_orders(self, product_id: Optional[int] = None) -> Dict:
        params = {"product_id": product_id} if product_id else None
        return await self._request("GET", "/v2/orders", params=params, auth=True)

    async def get_order_history(self, page_size: int = 50) -> Dict:
        return await self._request("GET", "/v2/orders/history",
                                   params={"page_size": page_size}, auth=True)

    async def place_order(self, product_id: int,
                          side: str, size: int,
                          order_type: str = "market_order",
                          limit_price: Optional[str] = None,
                          reduce_only: bool = False,
                          client_order_id: Optional[str] = None,
                          time_in_force: Optional[str] = None,
                          post_only: bool = False) -> Dict:
        """
        Place an order on Delta Exchange.

        Payload format matches the official delta-rest-client:
          - product_id (int), size (int), side (str), order_type (str)
          - reduce_only / post_only as string "true"/"false"
          - NO product_symbol field (not part of the API spec)

        Pass `client_order_id` to make retries idempotent.
        """
        payload: Dict[str, Any] = {
            "product_id": product_id,
            "size": int(size),
            "side": side,
            "order_type": order_type,
            "post_only": "true" if post_only else "false",
            "reduce_only": "true" if reduce_only else "false",
        }
        if order_type == "limit_order" and limit_price is not None:
            payload["limit_price"] = str(limit_price)
        if client_order_id:
            payload["client_order_id"] = client_order_id
        if time_in_force:
            payload["time_in_force"] = time_in_force

        logger.info(
            f"Placing order: {side} {size} PID={product_id} ({order_type})"
            + (f" @ {limit_price}" if limit_price else "")
            + (f" coid={client_order_id}" if client_order_id else "")
        )
        result = await self._request("POST", "/v2/orders", data=payload, auth=True)
        if not (isinstance(result, dict) and (result.get("success") or result.get("result"))):
            logger.error(f"Order REJECTED by exchange: {result}")
        return result

    async def place_stop_entry_order(
        self, product_id: int, side: str, size: int,
        stop_price: float,
        order_type: str = "market_order",
        limit_price: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> Dict:
        """Place a conditional STOP order for breakout entry (NOT reduce-only).

        Uses stop_order_type='stop_loss_order' because on Delta Exchange:
          - stop_loss_order BUY:  triggers when price >= stop_price (rises to level)
          - stop_loss_order SELL: triggers when price <= stop_price (drops to level)

        This is what a breakout strategy needs: the order sits dormant
        until price *reaches* the breakout level from inside the range.

        Note: take_profit_order has the OPPOSITE trigger direction:
          - take_profit_order BUY: triggers when price <= stop_price → immediate exec!
        
        IMPORTANT: The stop_price must have sufficient distance from the
        current ask (for buy) / bid (for sell) or the exchange will reject
        with 'immediate_execution_stop_order'.
        """
        payload: Dict[str, Any] = {
            "product_id": product_id,
            "size": int(size),
            "side": side,
            "order_type": order_type,
            "stop_order_type": "stop_loss_order",
            "stop_price": str(stop_price),
            "reduce_only": "false",
        }
        if order_type == "limit_order" and limit_price is not None:
            payload["limit_price"] = str(limit_price)
        if client_order_id:
            payload["client_order_id"] = client_order_id

        logger.info(
            f"Placing STOP {side.upper()} entry | PID={product_id} | "
            f"stop={stop_price} | size={size}"
            + (f" | coid={client_order_id}" if client_order_id else "")
        )
        result = await self._request("POST", "/v2/orders", data=payload, auth=True)
        if not (isinstance(result, dict) and (result.get("success") or result.get("result"))):
            logger.error(f"Stop entry order REJECTED: {result}")
        return result

    async def cancel_order(self, product_id: int, order_id: int) -> Dict:
        """Cancel an open order. Arg order matches official client: (product_id, order_id)."""
        payload = {"id": order_id, "product_id": product_id}
        return await self._request("DELETE", "/v2/orders", data=payload, auth=True)

    async def cancel_all_orders(self, product_id: Optional[int] = None) -> Dict:
        payload = {"product_id": product_id} if product_id else {}
        return await self._request("DELETE", "/v2/orders/all", data=payload, auth=True)

    async def get_open_orders(self, product_id: Optional[int] = None) -> Dict:
        """Fetch all open/pending orders from the exchange."""
        params = {}
        if product_id:
            params["product_id"] = product_id
        params["state"] = "open"
        return await self._request("GET", "/v2/orders", params=params, auth=True)

    async def close_position(self, product_id: int,
                             current_side: str, size: int) -> Dict:
        """Market-close an existing position."""
        close_side = "sell" if current_side == "LONG" else "buy"
        return await self.place_order(
            product_id=product_id,
            side=close_side,
            size=size,
            order_type="market_order",
            reduce_only=True,
        )

    async def place_bracket_order(self, product_id: int,
                                  stop_loss_price: float,
                                  take_profit_price: float,
                                  trail_amount: Optional[float] = None,
                                  bracket_stop_trigger_method: str = "mark_price") -> Dict:
        """Attach SL/TP bracket to an existing position via PUT /v2/orders/bracket.

        Uses the nested stop_loss_order / take_profit_order format required by
        the Delta Exchange API.  Market-order exits are used so we guarantee
        a fill when the trigger price is hit.
        """
        sl_order: Dict[str, Any] = {
            "order_type": "market_order",
            "stop_price": str(stop_loss_price),
        }
        if trail_amount:
            sl_order["trail_amount"] = str(trail_amount)

        tp_order: Dict[str, Any] = {
            "order_type": "market_order",
            "stop_price": str(take_profit_price),
        }

        payload: Dict[str, Any] = {
            "product_id": product_id,
            "stop_loss_order": sl_order,
            "take_profit_order": tp_order,
            "bracket_stop_trigger_method": bracket_stop_trigger_method,
        }

        logger.info(
            f"Placing bracket (PUT) | PID={product_id} | SL={stop_loss_price} | TP={take_profit_price}"
        )
        return await self._request("PUT", "/v2/orders/bracket", data=payload, auth=True)

    async def edit_bracket_order(self, product_id: int,
                                 stop_loss_price: Optional[float] = None,
                                 take_profit_price: Optional[float] = None,
                                 bracket_stop_trigger_method: str = "mark_price") -> Dict:
        """Edit an existing bracket SL/TP on a position (PUT /v2/orders/bracket)."""
        payload: Dict[str, Any] = {
            "product_id": product_id,
            "bracket_stop_trigger_method": bracket_stop_trigger_method,
        }
        if stop_loss_price is not None:
            payload["stop_loss_order"] = {
                "order_type": "market_order",
                "stop_price": str(stop_loss_price),
            }
        if take_profit_price is not None:
            payload["take_profit_order"] = {
                "order_type": "market_order",
                "stop_price": str(take_profit_price),
            }
        logger.info(f"Editing bracket | PID={product_id} | SL={stop_loss_price} | TP={take_profit_price}")
        return await self._request("PUT", "/v2/orders/bracket", data=payload, auth=True)

    async def place_stop_order(self, product_id: int,
                               side: str, size: int,
                               stop_price: float,
                               order_type: str = "market_order",
                               limit_price: Optional[str] = None) -> Dict:
        """Place an individual stop-loss order as a fallback."""
        payload: Dict[str, Any] = {
            "product_id": product_id,
            "size": int(size),
            "side": side,
            "order_type": order_type,
            "stop_order_type": "stop_loss_order",
            "stop_price": str(stop_price),
            "reduce_only": "true",
        }
        if order_type == "limit_order" and limit_price:
            payload["limit_price"] = str(limit_price)

        logger.info(f"Placing stop-loss order | {side} {size} PID={product_id} @ stop={stop_price}")
        return await self._request("POST", "/v2/orders", data=payload, auth=True)

    async def place_take_profit_order(self, product_id: int,
                                      side: str, size: int,
                                      stop_price: float,
                                      order_type: str = "market_order",
                                      limit_price: Optional[str] = None) -> Dict:
        """Place an individual take-profit order as a fallback."""
        payload: Dict[str, Any] = {
            "product_id": product_id,
            "size": int(size),
            "side": side,
            "order_type": order_type,
            "stop_order_type": "take_profit_order",
            "stop_price": str(stop_price),
            "reduce_only": "true",
        }
        if order_type == "limit_order" and limit_price:
            payload["limit_price"] = str(limit_price)

        logger.info(f"Placing take-profit order | {side} {size} PID={product_id} @ stop={stop_price}")
        return await self._request("POST", "/v2/orders", data=payload, auth=True)

    async def get_order_by_id(self, order_id: int) -> Dict:
        return await self._request("GET", f"/v2/orders/{order_id}", auth=True)

    async def cancel_product_orders(self, product_id: int) -> Dict:
        return await self.cancel_all_orders(product_id=product_id)

    async def set_leverage(self, product_id: int, leverage: int) -> Dict:
        """Set leverage for a product. URL matches official client."""
        payload = {"leverage": str(leverage)}
        return await self._request(
            "POST", f"/v2/products/{product_id}/orders/leverage",
            data=payload, auth=True,
        )

    # ─── Utility Methods ──────────────────────

    async def get_product_id(self, symbol: str) -> Optional[int]:
        result = await self.get_product_by_symbol(symbol)
        if result.get("success") and result.get("result"):
            return result["result"]["id"]
        return None

    async def get_24h_high_low(self, symbol: str) -> Optional[Dict[str, float]]:
        result = await self.get_ticker(symbol)
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
