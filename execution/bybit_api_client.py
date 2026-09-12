"""
Bybit API client for SignalCopy trader.

Extracted and simplified from basket_live_executor.py for mainnet futures trading.
"""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


class BybitAPIError(Exception):
    """Base exception for Bybit API errors."""
    pass


class BybitClient:
    """Lightweight Bybit V5 API client for futures trading."""
    
    BASE_URL = "https://api.bybit.com"
    RECV_WINDOW = 5000
    
    def __init__(self, api_key: str = None, api_secret: str = None):
        """Initialize Bybit client with credentials from env or params."""
        self.api_key = api_key or os.getenv("BYBIT_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BYBIT_SECRET", "")
        
        if not self.api_key or not self.api_secret:
            raise BybitAPIError("BYBIT_API_KEY and BYBIT_SECRET required")
    
    def _sign(self, params_str: str, timestamp: str) -> str:
        """Generate HMAC SHA256 signature."""
        sign_str = f"{timestamp}{self.api_key}{self.RECV_WINDOW}{params_str}"
        return hmac.new(
            self.api_secret.encode('utf-8'),
            sign_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
    
    def _request(
        self,
        endpoint: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        private: bool = False
    ) -> Dict[str, Any]:
        """Make HTTP request to Bybit API."""
        url = f"{self.BASE_URL}{endpoint}"
        params = params or {}
        
        if method == "GET" and params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"
        
        headers = {"Content-Type": "application/json"}
        
        if private:
            timestamp = str(int(time.time() * 1000))
            
            if method == "POST":
                params_str = json.dumps(params) if params else ""
            else:
                params_str = urllib.parse.urlencode(sorted(params.items())) if params else ""
            
            signature = self._sign(params_str, timestamp)
            
            headers.update({
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": signature,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": str(self.RECV_WINDOW)
            })
        
        request_data = None
        if method == "POST" and params:
            request_data = json.dumps(params).encode('utf-8')
        
        request = urllib.request.Request(
            url,
            data=request_data,
            headers=headers,
            method=method
        )
        
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.loads(response.read().decode('utf-8'))
                
                # Add response timestamp for freshness checks
                result["_response_ts"] = time.time()
                
                if result.get("retCode") != 0:
                    raise BybitAPIError(
                        f"API error {result.get('retCode')}: {result.get('retMsg')}"
                    )
                
                return result.get("result", result)
        
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            raise BybitAPIError(f"HTTP {e.code}: {error_body}")
        except urllib.error.URLError as e:
            raise BybitAPIError(f"Network error: {e.reason}")
        except Exception as e:
            raise BybitAPIError(f"Request failed: {str(e)}")
    
    # Public endpoints
    
    def get_ticker(self, symbol: str, category: str = "linear") -> Dict[str, Any]:
        """Get latest ticker price."""
        result = self._request("/v5/market/tickers", params={"category": category, "symbol": symbol})
        if not result.get("list"):
            raise BybitAPIError(f"No ticker data for {symbol}")
        return result["list"][0]
    
    def get_instrument_info(self, symbol: str, category: str = "linear") -> Dict[str, Any]:
        """Get instrument specifications (tick size, lot size, etc)."""
        result = self._request("/v5/market/instruments-info", params={"category": category, "symbol": symbol})
        if not result.get("list"):
            raise BybitAPIError(f"No instrument info for {symbol}")
        return result["list"][0]
    
    # Private endpoints
    
    def create_order(
        self,
        symbol: str,
        side: str,  # "Buy" or "Sell"
        order_type: str,  # "Market" or "Limit"
        qty: str,
        price: str = None,
        time_in_force: str = "GTC",
        position_idx: int = 0,
        take_profit: str = None,
        stop_loss: str = None,
        reduce_only: bool = False,
        order_link_id: str = None,
        category: str = "linear"
    ) -> Dict[str, Any]:
        """Place new order."""
        params = {
            "category": category,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": qty,
            "timeInForce": time_in_force,
            "positionIdx": position_idx
        }
        
        if reduce_only:
            params["reduceOnly"] = True
        
        if price:
            params["price"] = price
        if take_profit:
            params["takeProfit"] = take_profit
            params["tpTriggerBy"] = "MarkPrice"
        if stop_loss:
            params["stopLoss"] = stop_loss
            params["slTriggerBy"] = "MarkPrice"
        if order_link_id:
            params["orderLinkId"] = order_link_id
        
        return self._request("/v5/order/create", method="POST", params=params, private=True)
    
    def cancel_order(
        self,
        symbol: str,
        order_id: str = None,
        order_link_id: str = None,
        category: str = "linear"
    ) -> Dict[str, Any]:
        """Cancel existing order."""
        params = {"category": category, "symbol": symbol}
        
        if order_id:
            params["orderId"] = order_id
        elif order_link_id:
            params["orderLinkId"] = order_link_id
        else:
            raise BybitAPIError("Either order_id or order_link_id required")
        
        return self._request("/v5/order/cancel", method="POST", params=params, private=True)
    
    def get_open_orders(self, symbol: str = None, category: str = "linear") -> list:
        """Get open orders."""
        params = {"category": category, "openOnly": 0}
        if symbol:
            params["symbol"] = symbol
        
        result = self._request("/v5/order/realtime", params=params, private=True)
        return result.get("list", [])
    
    def get_order_history(
        self,
        symbol: str = None,
        order_id: str = None,
        order_link_id: str = None,
        category: str = "linear",
        limit: int = 20
    ) -> list:
        """Get order history (filled/cancelled orders).
        
        Args:
            symbol: Filter by symbol
            order_id: Filter by order ID
            order_link_id: Filter by order link ID
            category: linear/spot/inverse
            limit: Max orders to return (default 20, max 50)
        
        Returns:
            List of order dicts with fill info
        """
        params = {"category": category, "limit": limit}
        
        if symbol:
            params["symbol"] = symbol
        if order_id:
            params["orderId"] = order_id
        if order_link_id:
            # Bybit V5 uses snake_case for query parameters
            params["orderLinkId"] = order_link_id
        
        result = self._request("/v5/order/history", params=params, private=True)
        return result.get("list", [])
    
    def get_positions(self, symbol: str = None, category: str = "linear") -> list:
        """Get current positions."""
        params = {"category": category, "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        
        result = self._request("/v5/position/list", params=params, private=True)
        return result.get("list", [])
    
    def set_trading_stop(
        self,
        symbol: str,
        position_idx: int = 0,
        take_profit: str = None,
        stop_loss: str = None,
        category: str = "linear"
    ) -> Dict[str, Any]:
        """Update TP/SL for existing position."""
        params = {
            "category": category,
            "symbol": symbol,
            "positionIdx": position_idx
        }
        
        if take_profit:
            params["takeProfit"] = take_profit
            params["tpTriggerBy"] = "MarkPrice"
        if stop_loss:
            params["stopLoss"] = stop_loss
            params["slTriggerBy"] = "MarkPrice"
        
        return self._request("/v5/position/trading-stop", method="POST", params=params, private=True)
    
    def get_wallet_balance(self, account_type: str = "UNIFIED") -> Dict[str, Any]:
        """Get wallet balance."""
        result = self._request(
            "/v5/account/wallet-balance",
            params={"accountType": account_type},
            private=True
        )
        return result.get("list", [{}])[0]
