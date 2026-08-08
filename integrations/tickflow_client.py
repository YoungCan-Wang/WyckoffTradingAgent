"""
TickFlow 行情客户端（带重试与超时控制）。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests

from integrations.tickflow_notice import (
    TICKFLOW_LIMIT_HINT,
    is_tickflow_rate_limited_error,
    record_tickflow_limit_event,
)
from utils.env import env_bool

logger = logging.getLogger(__name__)

TICKFLOW_BASE_URL = "https://api.tickflow.org"
TICKFLOW_TIMEOUT_SECONDS = max(int(os.getenv("TICKFLOW_TIMEOUT_SECONDS", "12")), 3)
TICKFLOW_MAX_RETRIES = max(int(os.getenv("TICKFLOW_MAX_RETRIES", "3")), 1)
TICKFLOW_RETRY_BACKOFF_SECONDS = max(float(os.getenv("TICKFLOW_RETRY_BACKOFF_SECONDS", "1.5")), 0.1)
TICKFLOW_RATE_LIMIT_MAX_SLEEP_SECONDS = max(float(os.getenv("TICKFLOW_RATE_LIMIT_MAX_SLEEP_SECONDS", "90")), 1.0)
TICKFLOW_KLINE_RATE_LIMIT_PER_MIN = max(int(os.getenv("TICKFLOW_KLINE_RATE_LIMIT_PER_MIN", "0")), 0)
_TICKFLOW_QUOTES_MAX_SYMBOLS = 50
TICKFLOW_QUOTES_BATCH_SIZE = min(
    max(int(os.getenv("TICKFLOW_QUOTES_BATCH_SIZE", str(_TICKFLOW_QUOTES_MAX_SYMBOLS))), 1),
    _TICKFLOW_QUOTES_MAX_SYMBOLS,
)
TICKFLOW_QUOTES_BATCH_SLEEP = max(float(os.getenv("TICKFLOW_QUOTES_BATCH_SLEEP", "0.25")), 0.0)
TICKFLOW_KLINE_BATCH_SIZE = max(int(os.getenv("TICKFLOW_KLINE_BATCH_SIZE", "200")), 1)
TICKFLOW_KLINE_BATCH_SLEEP = max(float(os.getenv("TICKFLOW_KLINE_BATCH_SLEEP", "0.55")), 0.0)
TICKFLOW_INTRADAY_BATCH_SIZE = max(int(os.getenv("TICKFLOW_INTRADAY_BATCH_SIZE", "200")), 1)
TICKFLOW_INTRADAY_BATCH_SLEEP = max(float(os.getenv("TICKFLOW_INTRADAY_BATCH_SLEEP", "1.05")), 0.0)
TICKFLOW_FINANCIAL_BATCH_SIZE = max(int(os.getenv("TICKFLOW_FINANCIAL_BATCH_SIZE", "100")), 1)
TICKFLOW_FINANCIAL_BATCH_SLEEP = max(float(os.getenv("TICKFLOW_FINANCIAL_BATCH_SLEEP", "2.1")), 0.0)

_PERIOD_SET = {"1m", "5m", "10m", "15m", "30m", "60m", "1d", "1w", "1M", "1Q", "1Y"}
_CN_TZ = "Asia/Shanghai"
_ADJUST_SET = {"none", "forward", "backward", "forward_additive", "backward_additive"}
_RATE_LIMIT_WAIT_RE = re.compile(r"请\s*(\d+(?:\.\d+)?)\s*(ms|毫秒|s|秒)?\s*后重试", re.IGNORECASE)
_KLINE_CALL_TIMES: deque[float] = deque()
_KLINE_RATE_LOCK = threading.Lock()
_TICKFLOW_LOG_VERBOSE = env_bool("TICKFLOW_LOG_VERBOSE", False)


def _tf_log(msg: str, *, always: bool = False) -> None:
    if always or _TICKFLOW_LOG_VERBOSE:
        logger.info("%s", msg)


def _summarize_params(params: dict[str, Any] | None) -> str:
    if not params:
        return "-"
    out: list[str] = []
    for key, value in params.items():
        if key in {"symbols", "universes"}:
            if isinstance(value, (list, tuple, set)):
                items = [str(x).strip() for x in value if str(x).strip()]
            else:
                items = [x.strip() for x in str(value or "").split(",") if x.strip()]
            head = ",".join(items[:3])
            suffix = "..." if len(items) > 3 else ""
            out.append(f"{key}={len(items)}[{head}{suffix}]")
            continue
        text = str(value)
        if len(text) > 80:
            text = text[:77] + "..."
        out.append(f"{key}={text}")
    return "; ".join(out)


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _sleep_between_chunks(index: int, total: int, sleep_s: float) -> None:
    if index < total and sleep_s > 0:
        time.sleep(sleep_s)


def _send_http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None,
    json_body: dict[str, Any] | None,
    timeout_seconds: int,
) -> requests.Response:
    if method == "POST":
        return requests.post(url, headers=headers, json=json_body, timeout=timeout_seconds)
    return requests.get(url, headers=headers, params=params, timeout=timeout_seconds)


def _success_payload(
    resp: requests.Response,
    *,
    path: str,
    attempt: int,
    max_retries: int,
    started: float,
    params_summary: str,
) -> dict[str, Any]:
    elapsed = (time.monotonic() - started) * 1000
    prefix = f"recover ok path={path} attempt={attempt}/{max_retries}" if attempt > 1 else f"ok path={path}"
    _tf_log(f"{prefix} elapsed_ms={elapsed:.0f} params={params_summary}", always=attempt > 1)
    return resp.json()


def _retry_or_raise_http(
    resp: requests.Response,
    *,
    path: str,
    attempt: int,
    max_retries: int,
    params_summary: str,
    retry_backoff_seconds: float,
) -> None:
    body = (resp.text or "").strip()
    if resp.status_code == 429 or "rate_limited" in body.lower():
        record_tickflow_limit_event(body)
        _tf_log(
            f"rate_limited path={path} attempt={attempt}/{max_retries} params={params_summary} body={body[:160]}",
            always=True,
        )
        delay = _rate_limit_delay_seconds(body, resp.headers.get("Retry-After"))
        if attempt < max_retries and delay is not None:
            _tf_log(
                f"rate_limited_sleep path={path} attempt={attempt}/{max_retries} "
                f"sleep_s={delay:.1f} params={params_summary}",
                always=True,
            )
            time.sleep(delay)
            return
        raise RuntimeError(f"TickFlow HTTP 429: {body[:200]}（{TICKFLOW_LIMIT_HINT}）")

    if attempt < max_retries and (resp.status_code >= 500 or "error code: 1010" in body.lower()):
        _tf_log(
            f"retryable_http path={path} status={resp.status_code} "
            f"attempt={attempt}/{max_retries} params={params_summary}",
            always=True,
        )
        time.sleep(retry_backoff_seconds * attempt)
        return

    _tf_log(
        f"http_fail path={path} status={resp.status_code} "
        f"attempt={attempt}/{max_retries} params={params_summary} body={body[:160]}",
        always=True,
    )
    raise RuntimeError(f"TickFlow HTTP {resp.status_code}: {body[:200]}")


def _rate_limit_delay_seconds(body: str, retry_after: str | None) -> float | None:
    if retry_after:
        try:
            seconds = float(retry_after)
        except ValueError:
            seconds = 0.0
        if seconds > 0:
            return min(max(seconds, 0.1), TICKFLOW_RATE_LIMIT_MAX_SLEEP_SECONDS)

    match = _RATE_LIMIT_WAIT_RE.search(body)
    if not match:
        return None
    value = float(match.group(1))
    unit = str(match.group(2) or "ms").lower()
    if unit in {"ms", "毫秒"}:
        value /= 1000.0
    return min(max(value + 0.5, 0.1), TICKFLOW_RATE_LIMIT_MAX_SLEEP_SECONDS)


def _throttle_kline_request(path: str) -> None:
    limit = TICKFLOW_KLINE_RATE_LIMIT_PER_MIN
    if limit <= 0 or not path.startswith("/v1/klines"):
        return

    while True:
        now = time.monotonic()
        with _KLINE_RATE_LOCK:
            while _KLINE_CALL_TIMES and now - _KLINE_CALL_TIMES[0] >= 60.0:
                _KLINE_CALL_TIMES.popleft()
            if len(_KLINE_CALL_TIMES) < limit:
                _KLINE_CALL_TIMES.append(now)
                return
            sleep_s = max(60.0 - (now - _KLINE_CALL_TIMES[0]) + 0.05, 0.1)
        _tf_log(f"client_rate_limit_sleep path={path} sleep_s={sleep_s:.1f} limit={limit}/min", always=True)
        time.sleep(sleep_s)


def normalize_cn_symbol(raw: str) -> str:
    """将 A 股 6 位代码标准化为 TickFlow 接口格式：.SH / .SZ / .BJ。"""
    s = str(raw or "").strip().upper()
    if not s:
        return ""
    if "." in s and len(s.split(".", 1)[0]) == 6:
        return s
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) != 6:
        return s
    if digits.startswith(("0", "1", "2", "3")):
        return f"{digits}.SZ"
    if digits.startswith(("4", "8", "9")):
        return f"{digits}.BJ"
    return f"{digits}.SH"


def fetch_financial_metric_map(api_key: str, symbols: list[str]) -> dict[str, dict]:
    """取最新财务指标，按仓库统一的六位主键返回 {code6: MetricsRecord}。

    供应商后缀只存在于请求与响应边界；北交所 4/8/9 开头无法由六位代码反推交易所，
    业务层不得自行拼接后缀。
    """
    from core.candidate_metadata import code6

    raw = TickFlowClient(api_key=api_key).get_financial_metrics(symbols, latest=True)
    out: dict[str, dict] = {}
    for sym, records in raw.items():
        code = code6(sym)
        if code and records:
            out[code] = records[0]
    return out


def parse_ohlcv_payload(payload: dict[str, Any]) -> pd.DataFrame:
    """将 TickFlow K线 payload 转为标准 DataFrame。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return pd.DataFrame()
    ts = data.get("timestamp")
    if not isinstance(ts, list) or not ts:
        return pd.DataFrame()

    def _arr(name: str) -> list[float]:
        v = data.get(name)
        if isinstance(v, list):
            return v
        return [None] * len(ts)

    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": _arr("open"),
            "high": _arr("high"),
            "low": _arr("low"),
            "close": _arr("close"),
            "prev_close": _arr("prev_close"),
            "volume": _arr("volume"),
            "amount": _arr("amount"),
        }
    )
    for col in ("open", "high", "low", "close", "prev_close", "volume", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    dt = pd.to_datetime(df["timestamp"], unit="ms", utc=True, errors="coerce")
    df["datetime"] = dt.dt.tz_convert(_CN_TZ)
    df["date"] = df["datetime"].dt.date.astype(str)
    df = df.dropna(subset=["datetime", "close"]).sort_values("datetime").reset_index(drop=True)
    return df


@dataclass
class TickFlowClient:
    api_key: str
    base_url: str = TICKFLOW_BASE_URL
    timeout_seconds: int = TICKFLOW_TIMEOUT_SECONDS
    max_retries: int = TICKFLOW_MAX_RETRIES
    retry_backoff_seconds: float = TICKFLOW_RETRY_BACKOFF_SECONDS

    def __post_init__(self) -> None:
        self.api_key = str(self.api_key or "").strip()
        self.base_url = str(self.base_url or TICKFLOW_BASE_URL).strip().rstrip("/")
        self.timeout_seconds = max(int(self.timeout_seconds), 3)
        self.max_retries = max(int(self.max_retries), 1)
        self.retry_backoff_seconds = max(float(self.retry_backoff_seconds), 0.1)
        if not self.api_key:
            raise ValueError("TICKFLOW_API_KEY 未配置")

    def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        last_err: Exception | None = None
        url = f"{self.base_url}{path}"
        headers = {"x-api-key": self.api_key, **({"Content-Type": "application/json"} if json_body is not None else {})}
        params_summary = _summarize_params(json_body if json_body is not None else params)
        method_norm = str(method or "GET").strip().upper()
        for attempt in range(1, self.max_retries + 1):
            started = time.monotonic()
            try:
                _throttle_kline_request(path)
                resp = _send_http_request(
                    method_norm,
                    url,
                    headers=headers,
                    params=params,
                    json_body=json_body,
                    timeout_seconds=self.timeout_seconds,
                )
                if resp.status_code == 200:
                    return _success_payload(
                        resp,
                        path=path,
                        attempt=attempt,
                        max_retries=self.max_retries,
                        started=started,
                        params_summary=params_summary,
                    )
                _retry_or_raise_http(
                    resp,
                    path=path,
                    attempt=attempt,
                    max_retries=self.max_retries,
                    params_summary=params_summary,
                    retry_backoff_seconds=self.retry_backoff_seconds,
                )
                continue
            except Exception as e:  # requests.Timeout / requests.ConnectionError / RuntimeError
                if is_tickflow_rate_limited_error(e):
                    record_tickflow_limit_event(e)
                _tf_log(
                    f"request_error path={path} attempt={attempt}/{self.max_retries} "
                    f"params={params_summary} err={type(e).__name__}: {e}",
                    always=True,
                )
                last_err = e
                if attempt >= self.max_retries:
                    break
                time.sleep(self.retry_backoff_seconds * attempt)
        _tf_log(
            f"request_fail_final path={path} retries={self.max_retries} "
            f"params={params_summary} err={type(last_err).__name__ if last_err else 'Unknown'}: {last_err}",
            always=True,
        )
        raise RuntimeError(f"TickFlow 请求失败: {last_err}")

    def get_klines(
        self,
        symbol: str,
        *,
        period: str = "1d",
        count: int = 300,
        intraday: bool = False,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        adjust: str | None = None,
    ) -> pd.DataFrame:
        p = str(period or "1d").strip()
        if p not in _PERIOD_SET:
            raise ValueError(f"不支持的 period: {p}")
        endpoint = "/v1/klines/intraday" if intraday else "/v1/klines"
        params: dict[str, Any] = {
            "symbol": normalize_cn_symbol(symbol),
            "period": p,
            "count": max(int(count), 1),
        }
        if start_time_ms is not None:
            params["start_time"] = int(start_time_ms)
        if end_time_ms is not None:
            params["end_time"] = int(end_time_ms)
        if not intraday and adjust is not None:
            adj = str(adjust or "").strip().lower()
            if adj not in _ADJUST_SET:
                raise ValueError(f"不支持的 adjust: {adjust}")
            params["adjust"] = adj
        payload = self._request(
            endpoint,
            params=params,
        )
        return parse_ohlcv_payload(payload)

    def get_intraday(self, symbol: str, *, period: str = "1m", count: int = 500) -> pd.DataFrame:
        return self.get_klines(symbol, period=period, count=count, intraday=True)

    def get_klines_batch(
        self,
        symbols: list[str],
        *,
        period: str = "1d",
        count: int = 300,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        adjust: str | None = None,
    ) -> dict[str, pd.DataFrame]:
        """批量查询历史 K 线，返回 {symbol: DataFrame}。"""
        p = str(period or "1d").strip()
        if p not in _PERIOD_SET:
            raise ValueError(f"不支持的 period: {p}")
        clean = [normalize_cn_symbol(x) for x in symbols if str(x or "").strip()]
        clean = sorted({x for x in clean if x})
        if not clean:
            _tf_log("get_klines_batch skip: no valid symbols", always=True)
            return {}
        out: dict[str, pd.DataFrame] = {}
        chunks = _chunks(clean, TICKFLOW_KLINE_BATCH_SIZE)
        for index, chunk in enumerate(chunks, start=1):
            params: dict[str, Any] = {"symbols": ",".join(chunk), "period": p, "count": max(int(count), 1)}
            if start_time_ms is not None:
                params["start_time"] = int(start_time_ms)
            if end_time_ms is not None:
                params["end_time"] = int(end_time_ms)
            if adjust is not None:
                adj = str(adjust or "").strip().lower()
                if adj not in _ADJUST_SET:
                    raise ValueError(f"不支持的 adjust: {adjust}")
                params["adjust"] = adj
            payload = self._request("/v1/klines/batch", params=params)
            raw = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(raw, dict):
                for sym, kline_payload in raw.items():
                    symbol = normalize_cn_symbol(str(sym or "").strip())
                    if symbol and isinstance(kline_payload, dict):
                        out[symbol] = parse_ohlcv_payload({"data": kline_payload})
            _sleep_between_chunks(index, len(chunks), TICKFLOW_KLINE_BATCH_SLEEP)
        _tf_log(f"get_klines_batch done: received={len(out)}/{len(clean)}", always=True)
        return out

    def get_intraday_batch(
        self,
        symbols: list[str],
        *,
        period: str = "1m",
        count: int = 500,
    ) -> dict[str, pd.DataFrame]:
        """
        批量查询当日分时 K 线。
        接口: GET /v1/klines/intraday/batch
        返回: { "000001.SZ": DataFrame, ... }
        """
        p = str(period or "1m").strip()
        if p not in _PERIOD_SET:
            raise ValueError(f"不支持的 period: {p}")
        clean = [normalize_cn_symbol(x) for x in symbols if str(x or "").strip()]
        clean = sorted({x for x in clean if x})
        if not clean:
            _tf_log("get_intraday_batch skip: no valid symbols", always=True)
            return {}
        _tf_log(
            f"get_intraday_batch request symbols={len(clean)} period={p} count={max(int(count), 1)}",
            always=True,
        )

        out: dict[str, pd.DataFrame] = {}
        chunks = _chunks(clean, TICKFLOW_INTRADAY_BATCH_SIZE)
        for index, chunk in enumerate(chunks, start=1):
            payload = self._request(
                "/v1/klines/intraday/batch",
                params={"symbols": ",".join(chunk), "period": p, "count": max(int(count), 1)},
            )
            raw = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(raw, dict):
                for sym, kline_payload in raw.items():
                    symbol = normalize_cn_symbol(str(sym or "").strip())
                    if symbol and isinstance(kline_payload, dict):
                        out[symbol] = parse_ohlcv_payload({"data": kline_payload})
            _sleep_between_chunks(index, len(chunks), TICKFLOW_INTRADAY_BATCH_SLEEP)
        _tf_log(f"get_intraday_batch done: received={len(out)}/{len(clean)}", always=True)
        return out

    def get_depth(self, symbol: str) -> dict[str, Any]:
        """获取单个标的五档行情。返回 {bid_prices, bid_volumes, ask_prices, ask_volumes, timestamp}"""
        sym = normalize_cn_symbol(str(symbol or "").strip())
        if not sym:
            return {}
        resp = self._request("/v1/depth", params={"symbol": sym})
        return resp.get("data", {}) if isinstance(resp, dict) else {}

    def get_financial_metrics(self, symbols: list[str], *, latest: bool = True) -> dict[str, list[dict]]:
        """批量获取核心财务指标。返回 {vendor_symbol: [MetricsRecord]}

        主键为供应商格式（`300502.SZ`）。业务层请改用 `fetch_financial_metric_map()`，
        它按仓库统一的六位主键返回，避免下游按六位代码查询时静默取空。
        """
        clean = [normalize_cn_symbol(x) for x in symbols if str(x or "").strip()]
        clean = sorted({x for x in clean if x})
        if not clean:
            _tf_log("get_financial_metrics skip: no valid symbols", always=True)
            return {}
        out: dict[str, list[dict]] = {}
        chunks = _chunks(clean, TICKFLOW_FINANCIAL_BATCH_SIZE)
        for index, chunk in enumerate(chunks, start=1):
            _tf_log(f"get_financial_metrics request symbols={len(chunk)} latest={latest}", always=True)
            resp = self._request(
                "/v1/financials/metrics",
                params={"symbols": ",".join(chunk), "latest": "true" if latest else "false"},
            )
            data = resp.get("data") if isinstance(resp, dict) else None
            if isinstance(data, dict):
                for sym, records in data.items():
                    key = normalize_cn_symbol(str(sym).strip())
                    if key and isinstance(records, list):
                        out[key] = records
            _sleep_between_chunks(index, len(chunks), TICKFLOW_FINANCIAL_BATCH_SLEEP)
        _tf_log(f"get_financial_metrics done: received={len(out)}/{len(clean)}", always=True)
        return out

    def get_quotes(
        self,
        symbols: list[str] | None = None,
        *,
        universes: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        clean = [normalize_cn_symbol(x) for x in symbols or [] if str(x or "").strip()]
        clean = sorted({x for x in clean if x})
        universe_ids = sorted({str(x).strip() for x in universes or [] if str(x).strip()})
        if not clean and not universe_ids:
            return {}
        out: dict[str, dict[str, Any]] = {}
        bodies: list[dict[str, Any]] = []
        if universe_ids:
            bodies.append({"universes": universe_ids})
        bodies.extend({"symbols": chunk} for chunk in _chunks(clean, TICKFLOW_QUOTES_BATCH_SIZE))
        for index, body in enumerate(bodies, start=1):
            payload = self._request("/v1/quotes", json_body=body, method="POST")
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, list):
                for row in data:
                    if isinstance(row, dict):
                        sym = normalize_cn_symbol(str(row.get("symbol", "")).strip())
                        if sym:
                            out[sym] = row
            _sleep_between_chunks(index, len(bodies), TICKFLOW_QUOTES_BATCH_SLEEP)
        return out
