# -*- coding: utf-8 -*-

import json
import math
import os
import tempfile
import threading
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import click

from rqalpha import run_file


EXAMPLE_START_DATE = "2016-06-01"
EXAMPLE_END_DATE = "2016-08-31"
EXAMPLE_BENCHMARK = "000300.XSHG"
EXAMPLE_INITIAL_CASH = 100000
EXAMPLE_STRATEGY_NAME = "Built-in Example: buy_and_hold"


def _now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_float(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _format_date(value):
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value).split(" ")[0]


def _format_side(side, position_effect):
    side_map = {
        "BUY": "买入",
        "SELL": "卖出",
    }
    effect_map = {
        "OPEN": "开仓",
        "CLOSE": "平仓",
        "CLOSE_TODAY": "平今",
        "EXERCISE": "行权",
        "MATCH": "撮合",
    }
    side_text = side_map.get(str(side), str(side))
    effect_text = effect_map.get(str(position_effect), str(position_effect))
    if effect_text and effect_text != "None":
        return "{} / {}".format(side_text, effect_text)
    return side_text


def _format_trade_note(trade_record):
    transaction_cost = _safe_float(trade_record.get("transaction_cost"))
    commission = _safe_float(trade_record.get("commission"))
    tax = _safe_float(trade_record.get("tax"))
    return "手续费 {:.2f} · 佣金 {:.2f} · 印花税 {:.2f}".format(transaction_cost, commission, tax)


def adapt_sys_analyser_result(results):
    analyser_result = (results or {}).get("sys_analyser")
    if not analyser_result:
        raise RuntimeError("回测已执行，但没有生成 sys_analyser 结果")

    summary = analyser_result.get("summary") or {}
    portfolio = analyser_result.get("portfolio")
    trades = analyser_result.get("trades")
    benchmark_portfolio = analyser_result.get("benchmark_portfolio")

    if portfolio is None or getattr(portfolio, "empty", True):
        raise RuntimeError("回测结果中没有可展示的 portfolio 数据")

    benchmark_series = None
    if benchmark_portfolio is not None and not getattr(benchmark_portfolio, "empty", True):
        benchmark_series = benchmark_portfolio["unit_net_value"].reindex(portfolio.index).ffill()

    initial_cash = summary.get("stock")
    if initial_cash is None:
        initial_cash = EXAMPLE_INITIAL_CASH

    series = []
    for idx, row in portfolio.iterrows():
        strategy_value = _safe_float(row.get("unit_net_value"), 1.0)
        benchmark_value = strategy_value
        if benchmark_series is not None:
            benchmark_value = _safe_float(benchmark_series.loc[idx], strategy_value)
        series.append({
            "date": _format_date(idx),
            "strategy": round(strategy_value, 6),
            "benchmark": round(benchmark_value, 6),
        })

    trade_rows = []
    if trades is not None and not getattr(trades, "empty", True):
        for _, trade in trades.iterrows():
            trade_record = trade.to_dict()
            trade_rows.append({
                "date": str(trade_record.get("trading_datetime") or trade_record.get("datetime") or "").split(" ")[0],
                "symbol": trade_record.get("symbol") or trade_record.get("order_book_id") or "--",
                "side": _format_side(trade_record.get("side"), trade_record.get("position_effect")),
                "quantity": _safe_int(trade_record.get("last_quantity")),
                "price": round(_safe_float(trade_record.get("last_price")), 4),
                "note": _format_trade_note(trade_record),
            })

    return {
        "metadata": {
            "strategyName": summary.get("strategy_name") or EXAMPLE_STRATEGY_NAME,
            "startDate": summary.get("start_date") or EXAMPLE_START_DATE,
            "endDate": summary.get("end_date") or EXAMPLE_END_DATE,
            "initialCash": round(_safe_float(initial_cash), 2),
            "benchmarkName": summary.get("benchmark_symbol") or summary.get("benchmark") or EXAMPLE_BENCHMARK,
            "dataSourceLabel": "RQAlpha 本地 Web 服务 / sys_analyser",
            "generatedAt": _now_text(),
        },
        "kpis": {
            "totalReturnPct": round(_safe_float(summary.get("total_returns")) * 100, 2),
            "annualizedReturnPct": round(_safe_float(summary.get("annualized_returns")) * 100, 2),
            "maxDrawdownPct": round(-abs(_safe_float(summary.get("max_drawdown")) * 100), 2),
            "winRatePct": round(_safe_float(summary.get("win_rate")) * 100, 2),
            "sharpe": round(_safe_float(summary.get("sharpe")), 2),
        },
        "series": series,
        "trades": trade_rows,
    }


class WebRunState(object):
    def __init__(self):
        self._lock = threading.Lock()
        self._status = "idle"
        self._last_result = None
        self._last_error = None
        self._started_at = None
        self._finished_at = None
        self._last_run_label = EXAMPLE_STRATEGY_NAME

    def begin(self, run_label):
        with self._lock:
            if self._status == "running":
                return False
            self._status = "running"
            self._last_error = None
            self._started_at = _now_text()
            self._finished_at = None
            self._last_run_label = run_label
            return True

    def finish_success(self, result_payload):
        with self._lock:
            self._status = "success"
            self._last_result = result_payload
            self._last_error = None
            self._finished_at = _now_text()

    def finish_error(self, message):
        with self._lock:
            self._status = "error"
            self._last_error = message
            self._finished_at = _now_text()

    def snapshot(self):
        with self._lock:
            return {
                "status": self._status,
                "hasResult": self._last_result is not None,
                "lastError": self._last_error,
                "startedAt": self._started_at,
                "finishedAt": self._finished_at,
                "strategyName": self._last_run_label,
            }

    def last_result(self):
        with self._lock:
            return self._last_result


class RQAlphaWebApp(object):
    def __init__(self, host, port, data_bundle_path=None):
        self._host = host
        self._port = port
        self._state = WebRunState()
        self._package_root = Path(__file__).resolve().parent
        self._repo_root = self._package_root.parent
        self._web_root = self._repo_root / "web"
        self._example_strategy = self._package_root / "examples" / "buy_and_hold.py"
        self._data_bundle_path = self._resolve_bundle_path(data_bundle_path)

        if not self._web_root.exists():
            raise RuntimeError("未找到 web 目录：{}".format(self._web_root))
        if not self._example_strategy.exists():
            raise RuntimeError("未找到内置示例策略：{}".format(self._example_strategy))

    @property
    def state(self):
        return self._state

    @property
    def repo_root(self):
        return self._repo_root

    def _resolve_bundle_path(self, explicit_path):
        if explicit_path:
            return str(Path(explicit_path).expanduser().resolve())
        return os.path.expanduser("~/.rqalpha/bundle")

    def build_run_config(self, strategy_name):
        return {
            "base": {
                "start_date": EXAMPLE_START_DATE,
                "end_date": EXAMPLE_END_DATE,
                "data_bundle_path": self._data_bundle_path,
                "accounts": {
                    "stock": EXAMPLE_INITIAL_CASH,
                },
            },
            "extra": {
                "log_level": "error",
            },
            "mod": {
                "sys_analyser": {
                    "enabled": True,
                    "plot": False,
                    "benchmark": EXAMPLE_BENCHMARK,
                    "strategy_name": strategy_name,
                }
            }
        }

    def _bundle_error_message(self):
        return "运行失败：bundle path {} not exist；请先执行 rqalpha download-bundle，或用 --data-bundle-path 指定已有数据目录".format(self._data_bundle_path)

    def run_example_async(self):
        return self._start_run(str(self._example_strategy), EXAMPLE_STRATEGY_NAME)

    def run_uploaded_strategy_async(self, strategy_code, filename):
        safe_name = filename or "uploaded_strategy.py"
        run_label = "上传策略：{}".format(Path(safe_name).name)
        tmp_dir = Path(tempfile.gettempdir()) / "rqalpha-web-runs"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        strategy_path = tmp_dir / ("{}-{}".format(int(datetime.now().timestamp() * 1000), Path(safe_name).name))
        strategy_path.write_text(strategy_code, encoding="utf-8")
        return self._start_run(str(strategy_path), run_label)

    def _start_run(self, strategy_path, run_label):
        if not Path(self._data_bundle_path).exists():
            self._state.finish_error(self._bundle_error_message())
            return None
        if not self._state.begin(run_label):
            return False
        worker = threading.Thread(target=self._run_worker, args=(strategy_path, run_label), daemon=True)
        worker.start()
        return True

    def _run_worker(self, strategy_path, run_label):
        try:
            result = run_file(str(strategy_path), config=self.build_run_config(run_label))
            if result is None:
                raise RuntimeError("RQAlpha 没有返回结果")
            payload = adapt_sys_analyser_result(result)
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            self._state.finish_error("运行策略失败：{}".format(message))
            return
        self._state.finish_success(payload)

    def bundle_status(self):
        bundle_path = Path(self._data_bundle_path)
        return {
            "bundlePath": str(bundle_path),
            "bundleReady": bundle_path.exists(),
        }

    def serve_forever(self):
        handler = partial(RQAlphaWebHandler, directory=str(self._repo_root), app=self)
        with ThreadingHTTPServer((self._host, self._port), handler) as server:
            click.echo("RQAlpha Web UI 已启动：")
            click.echo("- 首页: http://{}:{}/web/index.html".format(self._host, self._port))
            click.echo("- 状态: http://{}:{}/api/status".format(self._host, self._port))
            server.serve_forever()


class RQAlphaWebHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self._app = kwargs.pop("app")
        super(RQAlphaWebHandler, self).__init__(*args, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            payload = self._app.state.snapshot()
            payload.update(self._app.bundle_status())
            self._send_json(payload)
            return
        if parsed.path == "/api/result":
            result = self._app.state.last_result()
            if result is None:
                self._send_json({"error": "当前还没有成功的回测结果"}, status=404)
            else:
                self._send_json(result)
            return
        super(RQAlphaWebHandler, self).do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/run-example", "/api/run-uploaded"):
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", "0") or 0)
        body = b""
        if content_length > 0:
            body = self.rfile.read(content_length)

        if parsed.path == "/api/run-example":
            accepted = self._app.run_example_async()
            if accepted is False:
                self._send_json({"error": "已有回测任务在运行中"}, status=409)
                return
            if accepted is None:
                self._send_json(self._app.state.snapshot(), status=400)
                return
            self._send_json({
                "status": "accepted",
                "message": "已开始运行内置示例策略"
            }, status=202)
            return

        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            self._send_json({"error": "上传策略请求不是有效的 JSON"}, status=400)
            return

        strategy_code = payload.get("strategyCode") or ""
        filename = payload.get("filename") or "uploaded_strategy.py"
        if not strategy_code.strip():
            self._send_json({"error": "没有收到策略代码"}, status=400)
            return

        accepted = self._app.run_uploaded_strategy_async(strategy_code, filename)
        if accepted is False:
            self._send_json({"error": "已有回测任务在运行中"}, status=409)
            return
        if accepted is None:
            self._send_json(self._app.state.snapshot(), status=400)
            return

        self._send_json({
            "status": "accepted",
            "message": "已开始运行上传策略"
        }, status=202)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
