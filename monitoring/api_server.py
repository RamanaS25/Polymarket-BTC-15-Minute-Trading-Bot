"""
Lightweight HTTP API server for frontend consumption.
Uses Python's built-in http.server to avoid new dependencies.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, Optional
from urllib.parse import urlparse, parse_qs

from loguru import logger

from monitoring.performance_tracker import get_performance_tracker
from execution.risk_engine import get_risk_engine
from execution.execution_engine import get_execution_engine


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def _tail_file(path: str, lines: int = 200) -> str:
    if lines <= 0:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            size = min(end, 64 * 1024)
            f.seek(-size, os.SEEK_END)
            chunk = f.read().decode("utf-8", errors="replace")
        return "\n".join(chunk.splitlines()[-lines:])
    except Exception as e:
        return f"Failed to read log file: {e}"


def _find_latest_log_file(log_dir: str) -> Optional[str]:
    if not os.path.isdir(log_dir):
        return None
    candidates = []
    for name in os.listdir(log_dir):
        if name.lower().endswith(".log"):
            path = os.path.join(log_dir, name)
            try:
                mtime = os.path.getmtime(path)
                candidates.append((mtime, path))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _load_paper_trades(path: str = "paper_trades.json") -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"count": 0, "wins": 0, "losses": 0, "win_rate": 0.0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        wins = sum(1 for t in data if str(t.get("outcome", "")).upper() == "WIN")
        losses = sum(1 for t in data if str(t.get("outcome", "")).upper() == "LOSS")
        count = len(data)
        win_rate = wins / count if count else 0.0
        return {"count": count, "wins": wins, "losses": losses, "win_rate": win_rate}
    except Exception as e:
        return {"count": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "error": str(e)}


@dataclass
class ApiContext:
    strategy: Any
    start_time: datetime
    simulation_default: bool
    test_mode: bool
    redis_client: Any = None
    log_dir: str = "./logs/nautilus"

    def _get_simulation_mode(self) -> Optional[bool]:
        if not self.redis_client:
            return None
        try:
            mode = self.redis_client.get("btc_trading:simulation_mode")
            if mode is None:
                return None
            return mode == "1"
        except Exception:
            return None

    def get_status(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        sim_override = self._get_simulation_mode()
        simulation = sim_override if sim_override is not None else self.simulation_default

        market_info = {}
        try:
            idx = self.strategy.current_instrument_index
            instruments = self.strategy.all_btc_instruments
            if instruments and 0 <= idx < len(instruments):
                m = instruments[idx]
                market_info = {
                    "slug": m.get("slug"),
                    "start_time": m.get("start_time"),
                    "end_time": m.get("end_time"),
                    "market_timestamp": m.get("market_timestamp"),
                }
                if m.get("end_time"):
                    market_info["seconds_to_close"] = int(
                        (m["end_time"] - now).total_seconds()
                    )
        except Exception:
            market_info = {}

        return {
            "timestamp": now.isoformat(),
            "uptime_seconds": int((now - self.start_time).total_seconds()),
            "mode": "simulation" if simulation else "live",
            "test_mode": self.test_mode,
            "five_sec_mode": getattr(self.strategy, "five_sec_mode", False),
            "learning_enabled": getattr(self.strategy, "learning_enabled", False),
            "redis_connected": self.redis_client is not None,
            "market": market_info,
            "price_history_points": len(getattr(self.strategy, "price_history", [])),
            "last_trade_time": getattr(self.strategy, "last_trade_time", None),
            "paper_trades": len(getattr(self.strategy, "paper_trades", [])),
        }

    def get_metrics(self) -> Dict[str, Any]:
        perf = get_performance_tracker()
        metrics = perf.calculate_metrics()
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "performance": {
                "total_pnl": float(metrics.total_pnl),
                "realized_pnl": float(metrics.realized_pnl),
                "unrealized_pnl": float(metrics.unrealized_pnl),
                "total_trades": metrics.total_trades,
                "win_rate": metrics.win_rate,
                "roi": metrics.roi,
                "sharpe_ratio": metrics.sharpe_ratio,
                "max_drawdown": metrics.max_drawdown,
            },
            "risk": get_risk_engine().get_risk_summary(),
            "execution": get_execution_engine().get_summary(),
        }

    def get_trades(self, limit: int = 100) -> Dict[str, Any]:
        perf = get_performance_tracker()
        trades = perf.get_trade_history(limit=limit)
        return {
            "count": len(trades),
            "trades": [
                {
                    "trade_id": t.trade_id,
                    "timestamp": t.timestamp.isoformat(),
                    "direction": t.direction,
                    "entry_price": float(t.entry_price),
                    "exit_price": float(t.exit_price),
                    "size": float(t.size),
                    "pnl": float(t.pnl),
                    "pnl_pct": t.pnl_pct,
                    "duration_seconds": t.duration_seconds,
                    "signal_score": t.signal_score,
                    "signal_confidence": t.signal_confidence,
                    "metadata": t.metadata,
                }
                for t in trades
            ],
        }

    def get_trade_stats(self) -> Dict[str, Any]:
        perf = get_performance_tracker()
        trades = perf.get_trade_history(limit=10000)
        wins = len([t for t in trades if t.pnl > 0])
        losses = len([t for t in trades if t.pnl < 0])
        total = len(trades)
        win_rate = wins / total if total else 0.0
        turnover = float(sum(t.size for t in trades)) if trades else 0.0
        total_pnl = float(sum(t.pnl for t in trades)) if trades else 0.0

        paper = _load_paper_trades()

        combined = {
            "count": total or paper["count"],
            "wins": wins or paper["wins"],
            "losses": losses or paper["losses"],
            "win_rate": win_rate if total else paper["win_rate"],
            "turnover_usd": turnover,
            "profit_loss_usd": total_pnl,
        }

        return {
            "performance_tracker": {
                "count": total,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "turnover_usd": turnover,
                "profit_loss_usd": total_pnl,
            },
            "paper_trades_file": paper,
            "combined": combined,
        }

    def get_config(self) -> Dict[str, Any]:
        return {
            "env": {
                "REDIS_HOST": os.getenv("REDIS_HOST", "localhost"),
                "REDIS_PORT": int(os.getenv("REDIS_PORT", 6379)),
                "REDIS_DB": int(os.getenv("REDIS_DB", 2)),
                "API_SERVER_PORT": int(os.getenv("API_SERVER_PORT", 8081)),
                "API_SERVER_HOST": os.getenv("API_SERVER_HOST", "127.0.0.1"),
            },
            "strategy": {
                "market_interval_seconds": 900,
                "fixed_position_size_usd": 1.0,
            },
        }

    def get_logs(self, lines: int = 200) -> Dict[str, Any]:
        log_file = _find_latest_log_file(self.log_dir)
        if not log_file:
            return {"path": None, "lines": "", "message": "No log file found"}
        return {
            "path": log_file,
            "lines": _tail_file(log_file, lines=lines),
        }

    def set_mode(self, simulation: bool) -> Dict[str, Any]:
        if not self.redis_client:
            return {"ok": False, "message": "Redis not configured"}
        try:
            self.redis_client.set("btc_trading:simulation_mode", "1" if simulation else "0")
            return {"ok": True, "mode": "simulation" if simulation else "live"}
        except Exception as e:
            return {"ok": False, "message": f"Failed to set mode: {e}"}


class ApiHandler(BaseHTTPRequestHandler):
    context: ApiContext = None

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Accept, Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Accept, Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path in ("/", ""):
            self._send_json(200, {"name": "Polymarket Bot API", "version": "v1"})
            return

        if path == "/health":
            self._send_json(200, {"status": "healthy"})
            return

        if path == "/api/v1/status":
            self._send_json(200, self.context.get_status())
            return

        if path == "/api/v1/metrics":
            self._send_json(200, self.context.get_metrics())
            return

        if path == "/api/v1/trades":
            limit = int(params.get("limit", ["100"])[0])
            self._send_json(200, self.context.get_trades(limit=limit))
            return

        if path == "/api/v1/config":
            self._send_json(200, self.context.get_config())
            return

        if path == "/api/v1/trade-stats":
            self._send_json(200, self.context.get_trade_stats())
            return

        if path == "/api/v1/logs":
            lines = int(params.get("lines", ["200"])[0])
            self._send_json(200, self.context.get_logs(lines=lines))
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/v1/mode":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                data = json.loads(body)
                simulation = bool(data.get("simulation", True))
                self._send_json(200, self.context.set_mode(simulation))
                return
            except Exception as e:
                self._send_json(400, {"ok": False, "message": f"Invalid body: {e}"})
                return

        self._send_json(404, {"error": "Not found"})

    def log_message(self, format, *args):
        return


class ApiServer:
    def __init__(self, context: ApiContext, host: str = "127.0.0.1", port: int = 8081):
        self.context = context
        self.host = host
        self.port = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._server:
            return
        ApiHandler.context = self.context
        self._server = HTTPServer((self.host, self.port), ApiHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info(f"API server listening on http://{self.host}:{self.port}")

    def stop(self) -> None:
        if not self._server:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        logger.info("API server stopped")
