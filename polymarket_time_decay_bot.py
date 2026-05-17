#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import plistlib
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GAMMA_KEYSET_URL = "https://gamma-api.polymarket.com/markets/keyset"
DEFAULT_DB_PATH = Path("data/polymarket_time_decay.sqlite3")
DEFAULT_STRATEGY_PATH = Path("config/time_decay_strategy.json")
DEFAULT_TELEGRAM_ENV_PATH = Path("config/telegram.env")
DEFAULT_POLYMARKET_ENV_PATH = Path("config/trading.env")
DEFAULT_LOG_PATH = Path("logs/polymarket_time_decay_bot.log")
DEFAULT_LAUNCHD_PLIST_PATH = Path("launchd/com.fernandozamora.polymarket-time-decay-bot.plist")
DEFAULT_LAUNCHD_LABEL = "com.fernandozamora.polymarket-time-decay-bot"
LOGGER_NAME = "polymarket_time_decay_bot"
DEFAULT_STRATEGY = {
    "required_keywords": [
        "announce",
        "announcement",
        "launch",
        "release",
        "tweet",
        "tweets",
        "post",
        "posts",
        "mention",
        "mentions",
        "publish",
        "publishes",
        "file",
        "files",
        "report",
        "reports",
        "reveal",
        "reveals",
        "confirm",
        "confirms",
        "speak",
        "speaks",
        "appear",
        "appears",
        "stream",
        "streams",
        "unveil",
        "unveils",
        "drop",
        "drops",
        "approve",
        "approves",
        "reject",
        "rejects",
        "resign",
        "resigns",
        "step down",
        "device",
        "album",
        "trailer",
    ],
    "blocked_keywords": [
        "temperature",
        "weather",
        "rain",
        "snow",
        "wind",
        "goal",
        "goals",
        "touchdown",
        "touchdowns",
        "points",
        "assists",
        "rebounds",
        "yards",
        "inning",
        "innings",
        "strikeout",
        "strikeouts",
        "bitcoin",
        "btc",
        "ethereum",
        "eth",
        "solana",
        "sol",
        "price",
        "market cap",
        "high temperature",
        "low temperature",
    ],
    "keyword_filters": [],
    "min_edge": 0.05,
    "min_confidence": 0.90,
    "max_entry_price": 0.97,
    "entry_price_buffer": 0.01,
    "trade_amount_usdc": 25.0,
    "max_trades_per_cycle": 1,
    "optimistic_yes_prior": 0.65,
    "yes_bias_buffer": 0.05,
    "min_hours_remaining": 0.25,
    "max_hours_remaining": 48.0,
    "min_elapsed_fraction": 0.60,
    "min_market_duration_hours": 6.0,
    "max_market_duration_hours": 168.0,
}
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PolymarketTimeDecayBot/1.0)",
    "Accept": "application/json",
}
HTTP_RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
HTTP_MAX_ATTEMPTS = 4
HTTP_RETRY_BACKOFF_SECONDS = 5.0
HTTP_RETRY_BACKOFF_CAP_SECONDS = 60.0
PAGE_REQUEST_DELAY_SECONDS = 0.25
TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class TimeDecayMarket:
    market_id: str
    event_id: str
    event_title: str
    event_slug: str
    question: str
    slug: str
    condition_id: str
    yes_price: float
    no_price: float
    yes_token_id: str
    no_token_id: str
    liquidity: float
    volume_24h: float
    fee_type: str
    tick_size: str
    neg_risk: bool
    description: str
    start_time: datetime
    end_time: datetime
    total_hours: float
    window_label: str
    search_text: str
    fetched_at: int


@dataclass(frozen=True)
class Opportunity:
    event_id: str
    event_title: str
    event_slug: str
    market_id: str
    question: str
    slug: str
    start_time: datetime
    end_time: datetime
    window_label: str
    hours_remaining: float
    total_hours: float
    elapsed_fraction: float
    baseline_yes_probability: float
    model_probability_yes: float
    model_probability_no: float
    market_yes_price: float
    market_no_price: float
    recommended_side: str
    current_side_price: float
    confidence: float
    edge: float
    limit_price: float
    share_size: float
    token_id: str
    tick_size: str
    neg_risk: bool
    liquidity: float
    volume_24h: float
    fetched_at: int


@dataclass(frozen=True)
class TradeExecution:
    market_id: str
    recommended_side: str
    order_id: str
    status: str
    created_at: int


@dataclass(frozen=True)
class CycleResult:
    fetched_at: int
    time_decay_market_count: int
    opportunities: list[Opportunity]
    trades: list[TradeExecution]
    summary_text: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Busca mercados narrativos de Polymarket con cierre diario o semanal y compra NO "
            "cuando el reloj ya ha drenado casi toda la probabilidad real del YES."
        )
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Segundos entre sondeos en modo watch o service. Recomendado: 60.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Ejecuta el bot en bucle. Sin este flag hace una sola pasada.",
    )
    parser.add_argument(
        "--service",
        action="store_true",
        help="Modo servicio: activa watch y habilita logging rotativo a archivo.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="Limita las iteraciones en modo watch. 0 significa infinito.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Cantidad máxima de oportunidades mostradas por iteración.",
    )
    parser.add_argument(
        "--limit-per-page",
        type=int,
        default=100,
        help="Tamaño de página para Gamma keyset. Máximo útil: 100.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Limita páginas por pasada para pruebas. 0 significa sin límite.",
    )
    parser.add_argument(
        "--keyword",
        action="append",
        default=None,
        help="Filtro adicional por palabra o frase. Se puede repetir.",
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help="Ruta al SQLite local usado para deduplicar alertas y órdenes.",
    )
    parser.add_argument(
        "--strategy-file",
        default=str(DEFAULT_STRATEGY_PATH),
        help="JSON con parámetros de sesgo temporal, filtros y sizing.",
    )
    parser.add_argument(
        "--min-liquidity",
        type=float,
        default=2000.0,
        help="Liquidez mínima del mercado binario para entrar al análisis.",
    )
    parser.add_argument(
        "--min-volume-24h",
        type=float,
        default=250.0,
        help="Volumen mínimo de 24h del mercado binario para entrar al análisis.",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=None,
        help="Ventaja mínima requerida sobre el precio del NO.",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=None,
        help="Confianza mínima exigida al NO modelado.",
    )
    parser.add_argument(
        "--max-entry-price",
        type=float,
        default=None,
        help="Precio máximo aceptado para comprar NO.",
    )
    parser.add_argument(
        "--entry-price-buffer",
        type=float,
        default=None,
        help="Colchón que se suma al precio observado para construir la orden límite.",
    )
    parser.add_argument(
        "--trade-amount-usdc",
        type=float,
        default=None,
        help="Capital a arriesgar por operación en USDC.",
    )
    parser.add_argument(
        "--max-trades-per-cycle",
        type=int,
        default=None,
        help="Máximo de órdenes nuevas por ciclo cuando la ejecución real está activa.",
    )
    parser.add_argument(
        "--optimistic-yes-prior",
        type=float,
        default=None,
        help="Probabilidad base generosa para el YES al inicio de la ventana.",
    )
    parser.add_argument(
        "--yes-bias-buffer",
        type=float,
        default=None,
        help="Puntos extra de optimismo que se añaden al YES observado para modelar sesgo.",
    )
    parser.add_argument(
        "--min-hours-remaining",
        type=float,
        default=None,
        help="Mínimo de horas restantes para operar. Evita entrar demasiado cerca del cierre.",
    )
    parser.add_argument(
        "--max-hours-remaining",
        type=float,
        default=None,
        help="Máximo de horas restantes para operar. Evita entrar demasiado pronto.",
    )
    parser.add_argument(
        "--min-elapsed-fraction",
        type=float,
        default=None,
        help="Porcentaje mínimo de la ventana que ya debe haberse consumido. Ejemplo: 0.6 = 60%%.",
    )
    parser.add_argument(
        "--min-market-duration-hours",
        type=float,
        default=None,
        help="Duración mínima total del mercado para considerarlo candidato.",
    )
    parser.add_argument(
        "--max-market-duration-hours",
        type=float,
        default=None,
        help="Duración máxima total del mercado para considerarlo candidato.",
    )
    parser.add_argument(
        "--execute-trades",
        action="store_true",
        help="Si está activo, envía órdenes reales a Polymarket usando py_clob_client_v2.",
    )
    parser.add_argument(
        "--polymarket-env-file",
        default=str(DEFAULT_POLYMARKET_ENV_PATH),
        help="Archivo .env para POLYMARKET_PRIVATE_KEY, funder, API keys y POLYMARKET_EXECUTE_TRADES.",
    )
    parser.add_argument(
        "--polymarket-host",
        default=os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com"),
        help="Host del CLOB de Polymarket.",
    )
    parser.add_argument(
        "--polymarket-chain-id",
        type=int,
        default=int(os.getenv("POLYMARKET_CHAIN_ID", "137")),
        help="Chain ID del CLOB. Polygon mainnet = 137.",
    )
    parser.add_argument(
        "--polymarket-private-key",
        default=os.getenv("POLYMARKET_PRIVATE_KEY", ""),
        help="Private key del signer usada para crear o derivar API keys del CLOB.",
    )
    parser.add_argument(
        "--polymarket-funder-address",
        default=os.getenv("POLYMARKET_FUNDER_ADDRESS", ""),
        help="Funder address para wallets proxy/deposit wallet. Opcional en EOA.",
    )
    parser.add_argument(
        "--polymarket-signature-type",
        type=int,
        default=None,
        help="Tipo de firma del cliente CLOB. 0=EOA, 3=deposit wallet, etc.",
    )
    parser.add_argument(
        "--polymarket-clob-api-key",
        default=os.getenv("POLYMARKET_CLOB_API_KEY", ""),
        help="API key L2 del CLOB. Si no se pasa, se deriva desde la private key.",
    )
    parser.add_argument(
        "--polymarket-clob-api-secret",
        default=os.getenv("POLYMARKET_CLOB_API_SECRET", ""),
        help="API secret L2 del CLOB.",
    )
    parser.add_argument(
        "--polymarket-clob-api-passphrase",
        default=os.getenv("POLYMARKET_CLOB_API_PASSPHRASE", ""),
        help="API passphrase L2 del CLOB.",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Habilita notificaciones de oportunidades nuevas por Telegram.",
    )
    parser.add_argument(
        "--telegram-env-file",
        default=str(DEFAULT_TELEGRAM_ENV_PATH),
        help="Archivo .env desde el que cargar POLYMARKET_TELEGRAM_BOT_TOKEN y POLYMARKET_TELEGRAM_CHAT_ID.",
    )
    parser.add_argument(
        "--telegram-bot-token",
        default=os.getenv("POLYMARKET_TELEGRAM_BOT_TOKEN", ""),
        help="Token del bot de Telegram. También se puede pasar por env.",
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=os.getenv("POLYMARKET_TELEGRAM_CHAT_ID", ""),
        help="Chat ID de Telegram. También se puede pasar por env.",
    )
    parser.add_argument(
        "--telegram-test-message",
        default="",
        help="Envía un mensaje de prueba a Telegram y sale.",
    )
    parser.add_argument(
        "--notify-top",
        type=int,
        default=3,
        help="Número máximo de oportunidades nuevas incluidas en cada notificación.",
    )
    parser.add_argument(
        "--notification-cooldown",
        type=int,
        default=1800,
        help="Segundos mínimos antes de reenviar una alerta para el mismo mercado y lado.",
    )
    parser.add_argument(
        "--log-file",
        default=str(DEFAULT_LOG_PATH),
        help="Archivo de log para modo service/watch.",
    )
    parser.add_argument(
        "--log-max-mb",
        type=float,
        default=5.0,
        help="Tamaño máximo en MB antes de rotar el log.",
    )
    parser.add_argument(
        "--log-backups",
        type=int,
        default=5,
        help="Cantidad de backups rotados a conservar.",
    )
    parser.add_argument(
        "--write-launchd-plist",
        action="store_true",
        help="Genera un plist de launchd para macOS y sale.",
    )
    parser.add_argument(
        "--launchd-plist-path",
        default=str(DEFAULT_LAUNCHD_PLIST_PATH),
        help="Ruta destino del plist de launchd.",
    )
    parser.add_argument(
        "--launchd-label",
        default=DEFAULT_LAUNCHD_LABEL,
        help="Label usado por launchd.",
    )
    return parser


def default_strategy_copy() -> dict[str, Any]:
    strategy = dict(DEFAULT_STRATEGY)
    strategy["required_keywords"] = list(DEFAULT_STRATEGY["required_keywords"])
    strategy["blocked_keywords"] = list(DEFAULT_STRATEGY["blocked_keywords"])
    strategy["keyword_filters"] = list(DEFAULT_STRATEGY["keyword_filters"])
    return strategy


def resolve_path(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def resolve_runtime_db_path(path_value: str) -> Path:
    db_path = resolve_path(path_value)
    default_db_path = resolve_path(str(DEFAULT_DB_PATH))
    if db_path == default_db_path and not db_path.exists():
        legacy_candidates = sorted(
            candidate
            for candidate in default_db_path.parent.glob("*.sqlite3")
            if candidate.name != default_db_path.name
        )
        if len(legacy_candidates) == 1:
            return legacy_candidates[0]
    return db_path


def load_simple_env_file(env_file_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_file_path.exists():
        return values

    with env_file_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            clean_key = key.strip()
            clean_value = value.strip().strip('"').strip("'")
            if clean_key:
                values[clean_key] = clean_value
    return values


def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in TRUE_ENV_VALUES
    return bool(value)


def parse_json_array(raw_value: Any) -> list[Any]:
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return raw_value
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def listify_strings(raw_value: Any) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        values = [raw_value]
    elif isinstance(raw_value, list):
        values = raw_value
    else:
        values = [str(raw_value)]
    normalized = []
    for value in values:
        text = normalize_whitespace(str(value)).strip()
        if text:
            normalized.append(text)
    return normalized


def hydrate_telegram_credentials(args: argparse.Namespace, env_values: dict[str, str]) -> None:
    if not args.telegram_bot_token:
        args.telegram_bot_token = env_values.get("POLYMARKET_TELEGRAM_BOT_TOKEN", "")
    if not args.telegram_chat_id:
        args.telegram_chat_id = env_values.get("POLYMARKET_TELEGRAM_CHAT_ID", "")


def hydrate_polymarket_credentials(args: argparse.Namespace, env_values: dict[str, str]) -> None:
    if not args.polymarket_private_key:
        args.polymarket_private_key = env_values.get("POLYMARKET_PRIVATE_KEY", "")
    if not args.polymarket_funder_address:
        args.polymarket_funder_address = env_values.get("POLYMARKET_FUNDER_ADDRESS", "")
    if args.polymarket_signature_type is None and env_values.get("POLYMARKET_SIGNATURE_TYPE"):
        args.polymarket_signature_type = int(env_values["POLYMARKET_SIGNATURE_TYPE"])
    if not args.polymarket_clob_api_key:
        args.polymarket_clob_api_key = env_values.get("POLYMARKET_CLOB_API_KEY", "")
    if not args.polymarket_clob_api_secret:
        args.polymarket_clob_api_secret = env_values.get("POLYMARKET_CLOB_API_SECRET", "")
    if not args.polymarket_clob_api_passphrase:
        args.polymarket_clob_api_passphrase = env_values.get("POLYMARKET_CLOB_API_PASSPHRASE", "")
    if not args.execute_trades and safe_bool(env_values.get("POLYMARKET_EXECUTE_TRADES")):
        args.execute_trades = True


def configure_logger(args: argparse.Namespace, log_file_path: Path) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    if args.watch or args.service:
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file_path,
            maxBytes=int(args.log_max_mb * 1024 * 1024),
            backupCount=args.log_backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(file_handler)

    return logger


def normalize_whitespace(text: str) -> str:
    return " ".join(str(text or "").split())


def normalize_search_text(*parts: str) -> str:
    return normalize_whitespace(" ".join(part for part in parts if part)).lower()


def keyword_match(text: str, keyword: str) -> bool:
    clean_keyword = normalize_whitespace(keyword).lower().strip()
    if not clean_keyword:
        return False
    if " " in clean_keyword:
        return clean_keyword in text
    return re.search(rf"\b{re.escape(clean_keyword)}\b", text) is not None


def matches_any_keyword(text: str, keywords: list[str]) -> bool:
    return any(keyword_match(text, keyword) for keyword in keywords)


def parse_retry_after_seconds(header_value: str | None) -> float | None:
    if not header_value:
        return None
    try:
        return max(0.0, float(header_value))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(header_value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def calculate_retry_delay(attempt_index: int, retry_after_header: str | None) -> float:
    retry_after_seconds = parse_retry_after_seconds(retry_after_header)
    if retry_after_seconds is not None:
        return retry_after_seconds
    return min(HTTP_RETRY_BACKOFF_CAP_SECONDS, HTTP_RETRY_BACKOFF_SECONDS * (2**attempt_index))


def fetch_json(url: str, params: dict[str, Any] | None = None) -> Any:
    if params:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        request_url = f"{url}?{query}"
    else:
        request_url = url

    logger = logging.getLogger(LOGGER_NAME)
    for attempt_index in range(HTTP_MAX_ATTEMPTS):
        request = Request(request_url, headers=HTTP_HEADERS)
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in HTTP_RETRY_STATUS_CODES:
                raise
            delay_seconds = calculate_retry_delay(attempt_index, exc.headers.get("Retry-After") if exc.headers else None)
            if attempt_index + 1 >= HTTP_MAX_ATTEMPTS:
                raise
            logger.warning(
                "La API devolvio HTTP %s. Reintentando en %.1fs (%s/%s).",
                exc.code,
                delay_seconds,
                attempt_index + 1,
                HTTP_MAX_ATTEMPTS - 1,
            )
            time.sleep(delay_seconds)


def post_form_json(url: str, data: dict[str, Any]) -> Any:
    encoded = urlencode({key: value for key, value in data.items() if value is not None}).encode("utf-8")
    request = Request(url, data=encoded, headers={**HTTP_HEADERS, "Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def iter_active_markets(limit: int, max_pages: int) -> list[dict[str, Any]]:
    cursor: str | None = None
    pages = 0
    markets: list[dict[str, Any]] = []

    while True:
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
        }
        if cursor:
            params["after_cursor"] = cursor
        payload = fetch_json(GAMMA_KEYSET_URL, params)
        page_markets = payload.get("markets", [])
        if not page_markets:
            break
        markets.extend(page_markets)
        pages += 1
        cursor = payload.get("next_cursor")
        if not cursor:
            break
        if max_pages and pages >= max_pages:
            break
        time.sleep(PAGE_REQUEST_DELAY_SECONDS)

    return markets


def parse_yes_no_prices(market: dict[str, Any]) -> tuple[float, float] | None:
    outcomes = parse_json_array(market.get("outcomes"))
    prices = parse_json_array(market.get("outcomePrices"))
    if len(outcomes) != 2 or len(prices) != 2:
        fallback_yes = market.get("lastTradePrice")
        if fallback_yes is None:
            return None
        yes_price = safe_float(fallback_yes)
        return yes_price, max(0.0, min(1.0, 1.0 - yes_price))

    mapping: dict[str, float] = {}
    for label, price in zip(outcomes, prices):
        if not isinstance(label, str):
            continue
        mapping[label.strip().lower()] = safe_float(price)

    if set(mapping) != {"yes", "no"}:
        return None
    return mapping["yes"], mapping["no"]


def parse_yes_no_token_ids(market: dict[str, Any]) -> tuple[str, str]:
    outcomes = parse_json_array(market.get("outcomes"))
    token_ids = parse_json_array(market.get("clobTokenIds"))
    mapping: dict[str, str] = {}
    for label, token_id in zip(outcomes, token_ids):
        if isinstance(label, str):
            mapping[label.strip().lower()] = str(token_id)
    return mapping.get("yes", ""), mapping.get("no", "")


def normalize_tick_size(raw_value: Any) -> str:
    tick_size = safe_float(raw_value, 0.01)
    if tick_size <= 0:
        tick_size = 0.01
    return f"{tick_size:.4f}".rstrip("0").rstrip(".")


def parse_iso_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_timestamp_like(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1_000_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", stripped):
            timestamp = float(stripped)
            if timestamp > 1_000_000_000_000:
                timestamp /= 1000.0
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        try:
            return parse_iso_datetime(stripped)
        except ValueError:
            return None
    return None


def coalesce_market_datetime(raw_market: dict[str, Any], event: dict[str, Any], field_names: list[str]) -> datetime | None:
    for field_name in field_names:
        for source in (raw_market, event):
            candidate = parse_timestamp_like(source.get(field_name))
            if candidate is not None:
                return candidate
    return None


def classify_window_label(text: str, total_hours: float) -> str:
    normalized = normalize_whitespace(text).lower()
    if "today" in normalized or "tonight" in normalized or total_hours <= 36.0:
        return "daily"
    if "this week" in normalized or total_hours <= 168.0:
        return "weekly"
    return "custom"


def extract_time_decay_market(raw_market: dict[str, Any], fetched_at: int) -> TimeDecayMarket | None:
    event_list = raw_market.get("events") or []
    if not event_list:
        return None
    yes_no_prices = parse_yes_no_prices(raw_market)
    if yes_no_prices is None:
        return None

    event = event_list[0]
    event_id = str(event.get("id") or "")
    market_id = str(raw_market.get("id") or "")
    if not event_id or not market_id:
        return None

    event_title = normalize_whitespace(str(event.get("title") or raw_market.get("question") or ""))
    question = normalize_whitespace(str(raw_market.get("question") or ""))
    if not question:
        return None

    start_time = coalesce_market_datetime(raw_market, event, ["startDate", "acceptingOrdersTimestamp", "createdAt"])
    end_time = coalesce_market_datetime(raw_market, event, ["endDate", "closedTime"])
    if start_time is None or end_time is None or end_time <= start_time:
        return None

    as_of = datetime.fromtimestamp(fetched_at, tz=timezone.utc)
    if end_time <= as_of:
        return None

    total_hours = (end_time - start_time).total_seconds() / 3600.0
    if total_hours <= 0:
        return None

    yes_price, no_price = yes_no_prices
    yes_token_id, no_token_id = parse_yes_no_token_ids(raw_market)
    description = normalize_whitespace(
        " ".join(
            [
                str(raw_market.get("description") or ""),
                str(event.get("description") or ""),
            ]
        )
    )
    search_text = normalize_search_text(
        question,
        event_title,
        description,
        str(raw_market.get("slug") or ""),
        str(event.get("slug") or ""),
    )

    return TimeDecayMarket(
        market_id=market_id,
        event_id=event_id,
        event_title=event_title,
        event_slug=str(event.get("slug") or ""),
        question=question,
        slug=str(raw_market.get("slug") or ""),
        condition_id=str(raw_market.get("conditionId") or ""),
        yes_price=yes_price,
        no_price=no_price,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        liquidity=safe_float(raw_market.get("liquidityNum") or raw_market.get("liquidity")),
        volume_24h=safe_float(raw_market.get("volume24hr") or raw_market.get("volume24hrClob")),
        fee_type=str(raw_market.get("feeType") or "unknown").strip().lower(),
        tick_size=normalize_tick_size(raw_market.get("orderPriceMinTickSize") or raw_market.get("minimum_tick_size") or raw_market.get("minimumTickSize")),
        neg_risk=safe_bool(raw_market.get("negRisk") or raw_market.get("neg_risk")),
        description=description,
        start_time=start_time,
        end_time=end_time,
        total_hours=total_hours,
        window_label=classify_window_label(f"{event_title} {question}", total_hours),
        search_text=search_text,
        fetched_at=fetched_at,
    )


def market_matches_strategy(market: TimeDecayMarket, strategy: dict[str, Any]) -> bool:
    required_keywords = listify_strings(strategy.get("required_keywords"))
    blocked_keywords = listify_strings(strategy.get("blocked_keywords"))
    keyword_filters = listify_strings(strategy.get("keyword_filters"))

    if blocked_keywords and matches_any_keyword(market.search_text, blocked_keywords):
        return False
    if required_keywords and not matches_any_keyword(market.search_text, required_keywords):
        return False
    if keyword_filters and not matches_any_keyword(market.search_text, keyword_filters):
        return False
    return True


def build_time_decay_markets(
    raw_markets: list[dict[str, Any]],
    fetched_at: int,
    min_liquidity: float,
    min_volume_24h: float,
    strategy: dict[str, Any],
) -> list[TimeDecayMarket]:
    min_market_duration_hours = safe_float(
        strategy.get("min_market_duration_hours"), DEFAULT_STRATEGY["min_market_duration_hours"]
    )
    max_market_duration_hours = safe_float(
        strategy.get("max_market_duration_hours"), DEFAULT_STRATEGY["max_market_duration_hours"]
    )
    as_of = datetime.fromtimestamp(fetched_at, tz=timezone.utc)

    markets: list[TimeDecayMarket] = []
    for raw_market in raw_markets:
        market = extract_time_decay_market(raw_market, fetched_at)
        if market is None:
            continue
        if market.start_time > as_of:
            continue
        if market.liquidity < min_liquidity:
            continue
        if market.volume_24h < min_volume_24h:
            continue
        if market.total_hours < min_market_duration_hours or market.total_hours > max_market_duration_hours:
            continue
        if not market_matches_strategy(market, strategy):
            continue
        markets.append(market)
    return markets


def load_strategy(strategy_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    strategy = default_strategy_copy()
    if strategy_path.exists():
        with strategy_path.open("r", encoding="utf-8") as handle:
            custom_strategy = json.load(handle)
        if not isinstance(custom_strategy, dict):
            raise ValueError("El archivo de estrategia debe ser un objeto JSON.")
        strategy.update(custom_strategy)

    numeric_overrides = {
        "min_edge": args.min_edge,
        "min_confidence": args.min_confidence,
        "max_entry_price": args.max_entry_price,
        "entry_price_buffer": args.entry_price_buffer,
        "trade_amount_usdc": args.trade_amount_usdc,
        "max_trades_per_cycle": args.max_trades_per_cycle,
        "optimistic_yes_prior": args.optimistic_yes_prior,
        "yes_bias_buffer": args.yes_bias_buffer,
        "min_hours_remaining": args.min_hours_remaining,
        "max_hours_remaining": args.max_hours_remaining,
        "min_elapsed_fraction": args.min_elapsed_fraction,
        "min_market_duration_hours": args.min_market_duration_hours,
        "max_market_duration_hours": args.max_market_duration_hours,
    }
    for key, value in numeric_overrides.items():
        if value is not None:
            strategy[key] = value

    strategy["required_keywords"] = listify_strings(strategy.get("required_keywords"))
    strategy["blocked_keywords"] = listify_strings(strategy.get("blocked_keywords"))
    keyword_filters = listify_strings(strategy.get("keyword_filters"))
    if args.keyword:
        keyword_filters.extend(listify_strings(args.keyword))
    strategy["keyword_filters"] = keyword_filters
    return strategy


def calculate_time_decay_yes_probability(baseline_yes_probability: float, remaining_fraction: float) -> float:
    bounded_baseline = min(0.999, max(0.0, baseline_yes_probability))
    bounded_fraction = min(1.0, max(0.0, remaining_fraction))
    if bounded_baseline <= 0.0 or bounded_fraction <= 0.0:
        return 0.0
    return 1.0 - math.pow(1.0 - bounded_baseline, bounded_fraction)


def round_price_to_tick(price: float, tick_size: str) -> float:
    tick = safe_float(tick_size, 0.01)
    if tick <= 0:
        tick = 0.01
    rounded = math.floor((price + 1e-9) / tick) * tick
    rounded = max(tick, rounded)
    return min(0.99, round(rounded, 6))


def evaluate_market(market: TimeDecayMarket, strategy: dict[str, Any]) -> Opportunity | None:
    as_of = datetime.fromtimestamp(market.fetched_at, tz=timezone.utc)
    hours_remaining = (market.end_time - as_of).total_seconds() / 3600.0
    if hours_remaining <= 0:
        return None

    min_hours_remaining = safe_float(strategy.get("min_hours_remaining"), DEFAULT_STRATEGY["min_hours_remaining"])
    max_hours_remaining = safe_float(strategy.get("max_hours_remaining"), DEFAULT_STRATEGY["max_hours_remaining"])
    if hours_remaining < min_hours_remaining or hours_remaining > max_hours_remaining:
        return None

    remaining_fraction = hours_remaining / market.total_hours
    elapsed_fraction = 1.0 - remaining_fraction
    min_elapsed_fraction = safe_float(strategy.get("min_elapsed_fraction"), DEFAULT_STRATEGY["min_elapsed_fraction"])
    if elapsed_fraction < min_elapsed_fraction:
        return None

    optimistic_yes_prior = safe_float(strategy.get("optimistic_yes_prior"), DEFAULT_STRATEGY["optimistic_yes_prior"])
    yes_bias_buffer = safe_float(strategy.get("yes_bias_buffer"), DEFAULT_STRATEGY["yes_bias_buffer"])
    baseline_yes_probability = min(0.999, max(optimistic_yes_prior, market.yes_price + max(0.0, yes_bias_buffer)))

    model_probability_yes = calculate_time_decay_yes_probability(baseline_yes_probability, remaining_fraction)
    model_probability_no = 1.0 - model_probability_yes
    edge = model_probability_no - market.no_price
    confidence = model_probability_no

    max_entry_price = safe_float(strategy.get("max_entry_price"), DEFAULT_STRATEGY["max_entry_price"])
    min_edge = safe_float(strategy.get("min_edge"), DEFAULT_STRATEGY["min_edge"])
    min_confidence = safe_float(strategy.get("min_confidence"), DEFAULT_STRATEGY["min_confidence"])
    entry_price_buffer = safe_float(strategy.get("entry_price_buffer"), DEFAULT_STRATEGY["entry_price_buffer"])
    trade_amount_usdc = safe_float(strategy.get("trade_amount_usdc"), DEFAULT_STRATEGY["trade_amount_usdc"])

    if edge < min_edge:
        return None
    if confidence < min_confidence:
        return None
    if market.no_price > max_entry_price:
        return None

    limit_price = round_price_to_tick(min(max_entry_price, market.no_price + entry_price_buffer), market.tick_size)
    share_size = round(trade_amount_usdc / limit_price, 6)
    if limit_price <= 0 or share_size <= 0:
        return None

    return Opportunity(
        event_id=market.event_id,
        event_title=market.event_title,
        event_slug=market.event_slug,
        market_id=market.market_id,
        question=market.question,
        slug=market.slug,
        start_time=market.start_time,
        end_time=market.end_time,
        window_label=market.window_label,
        hours_remaining=hours_remaining,
        total_hours=market.total_hours,
        elapsed_fraction=elapsed_fraction,
        baseline_yes_probability=baseline_yes_probability,
        model_probability_yes=model_probability_yes,
        model_probability_no=model_probability_no,
        market_yes_price=market.yes_price,
        market_no_price=market.no_price,
        recommended_side="NO",
        current_side_price=market.no_price,
        confidence=confidence,
        edge=edge,
        limit_price=limit_price,
        share_size=share_size,
        token_id=market.no_token_id,
        tick_size=market.tick_size,
        neg_risk=market.neg_risk,
        liquidity=market.liquidity,
        volume_24h=market.volume_24h,
        fetched_at=market.fetched_at,
    )


def ensure_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_alerts (
            signal_key TEXT PRIMARY KEY,
            sent_at INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            market_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            last_edge REAL NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS executed_orders (
            trade_key TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            market_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            token_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            status TEXT NOT NULL,
            response_json TEXT NOT NULL
        )
        """
    )
    connection.commit()


def opportunity_alert_key(opportunity: Opportunity) -> str:
    return f"{opportunity.market_id}:{opportunity.recommended_side}"


def should_send_alert(connection: sqlite3.Connection, opportunity: Opportunity, cooldown_seconds: int) -> bool:
    row = connection.execute(
        "SELECT sent_at FROM sent_alerts WHERE signal_key = ?",
        (opportunity_alert_key(opportunity),),
    ).fetchone()
    if row is None:
        return True
    last_sent_at = int(row[0])
    return opportunity.fetched_at - last_sent_at >= cooldown_seconds


def record_sent_alert(connection: sqlite3.Connection, opportunity: Opportunity) -> None:
    with connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO sent_alerts (
                signal_key,
                sent_at,
                event_id,
                market_id,
                direction,
                last_edge
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                opportunity_alert_key(opportunity),
                opportunity.fetched_at,
                opportunity.event_id,
                opportunity.market_id,
                opportunity.recommended_side,
                opportunity.edge,
            ),
        )


def trade_key(opportunity: Opportunity) -> str:
    return f"{opportunity.market_id}:{opportunity.recommended_side}"


def trade_already_recorded(connection: sqlite3.Connection, opportunity: Opportunity) -> bool:
    row = connection.execute(
        "SELECT 1 FROM executed_orders WHERE trade_key = ?",
        (trade_key(opportunity),),
    ).fetchone()
    return row is not None


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "__dict__"):
        return value.__dict__
    return str(value)


def extract_response_field(response: Any, *names: str) -> str:
    if isinstance(response, dict):
        for name in names:
            if response.get(name) is not None:
                return str(response[name])
    for name in names:
        value = getattr(response, name, None)
        if value is not None:
            return str(value)
    return ""


def record_executed_trade(connection: sqlite3.Connection, opportunity: Opportunity, response: Any) -> TradeExecution:
    order_id = extract_response_field(response, "orderID", "order_id")
    status = extract_response_field(response, "status")
    execution = TradeExecution(
        market_id=opportunity.market_id,
        recommended_side=opportunity.recommended_side,
        order_id=order_id,
        status=status,
        created_at=opportunity.fetched_at,
    )
    with connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO executed_orders (
                trade_key,
                created_at,
                event_id,
                market_id,
                direction,
                token_id,
                order_id,
                status,
                response_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_key(opportunity),
                opportunity.fetched_at,
                opportunity.event_id,
                opportunity.market_id,
                opportunity.recommended_side,
                opportunity.token_id,
                order_id,
                status,
                json.dumps(response, default=json_default, ensure_ascii=True),
            ),
        )
    return execution


def build_trading_client(args: argparse.Namespace) -> Any:
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Falta py-clob-client-v2. Instala dependencias con pip install -r requirements.txt.") from exc

    client_kwargs: dict[str, Any] = {
        "host": args.polymarket_host,
        "chain_id": args.polymarket_chain_id,
        "key": args.polymarket_private_key,
    }
    if args.polymarket_signature_type is not None:
        client_kwargs["signature_type"] = args.polymarket_signature_type
    if args.polymarket_funder_address:
        client_kwargs["funder"] = args.polymarket_funder_address

    if (
        args.polymarket_clob_api_key
        and args.polymarket_clob_api_secret
        and args.polymarket_clob_api_passphrase
    ):
        creds = ApiCreds(
            api_key=args.polymarket_clob_api_key,
            api_secret=args.polymarket_clob_api_secret,
            api_passphrase=args.polymarket_clob_api_passphrase,
        )
    else:
        temp_client = ClobClient(**client_kwargs)
        creds = temp_client.create_or_derive_api_key()

    return ClobClient(**{**client_kwargs, "creds": creds})


def place_limit_order(client: Any, opportunity: Opportunity) -> Any:
    from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

    return client.create_and_post_order(
        order_args=OrderArgs(
            token_id=opportunity.token_id,
            price=opportunity.limit_price,
            side=Side.BUY,
            size=opportunity.share_size,
        ),
        options=PartialCreateOrderOptions(
            tick_size=opportunity.tick_size,
            neg_risk=opportunity.neg_risk,
        ),
        order_type=OrderType.GTC,
    )


def format_timestamp(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_money(value: float) -> str:
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    if absolute >= 1_000_000:
        return f"{sign}${absolute / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{sign}${absolute / 1_000:.1f}K"
    return f"{sign}${absolute:.0f}"


def market_url(opportunity: Opportunity) -> str:
    if opportunity.event_slug:
        return f"https://polymarket.com/event/{opportunity.event_slug}"
    return "https://polymarket.com"


def render_opportunities(
    opportunities: list[Opportunity],
    time_decay_market_count: int,
    trades: list[TradeExecution],
    fetched_at: int,
    top: int,
) -> str:
    lines = [
        f"[{format_timestamp(fetched_at)}] Analizados {time_decay_market_count} mercados time_decay candidatos.",
    ]
    if not opportunities:
        lines.append("No hubo oportunidades NO que superaran edge, confianza y precio máximo en esta pasada.")
        return "\n".join(lines)

    lines.append(f"Oportunidades detectadas: {min(len(opportunities), top)} de {len(opportunities)}")
    for index, opportunity in enumerate(opportunities[:top], start=1):
        lines.append(
            (
                f"{index}. {opportunity.window_label} | quedan {opportunity.hours_remaining:.1f}h de {opportunity.total_hours:.1f}h | "
                f"NO modelo {opportunity.model_probability_no:.1%} | YES {opportunity.market_yes_price:.3f} / NO {opportunity.market_no_price:.3f} | "
                f"comprar NO a {opportunity.limit_price:.3f} | edge {opportunity.edge:.1%}"
            )
        )
        lines.append(
            (
                f"   elapsed {opportunity.elapsed_fraction:.1%} | baseline YES {opportunity.baseline_yes_probability:.1%} | "
                f"liquidez {format_money(opportunity.liquidity)} | volumen 24h {format_money(opportunity.volume_24h)} | "
                f"size {opportunity.share_size:.2f} shares"
            )
        )
        lines.append(f"   {opportunity.question}")
    if trades:
        lines.append(f"Ordenes enviadas: {len(trades)}")
        for trade in trades:
            order_suffix = f" | order {trade.order_id}" if trade.order_id else ""
            lines.append(f"   {trade.market_id} {trade.recommended_side} | {trade.status or 'submitted'}{order_suffix}")
    return "\n".join(lines)


def build_telegram_digest(opportunities: list[Opportunity]) -> str:
    lines = [
        "Polymarket Time Decay Bot",
        format_timestamp(opportunities[0].fetched_at),
        f"Oportunidades nuevas: {len(opportunities)}",
        "",
    ]
    for index, opportunity in enumerate(opportunities, start=1):
        lines.append(f"{index}. {opportunity.window_label} | quedan {opportunity.hours_remaining:.1f}h")
        lines.append(opportunity.question)
        lines.append(
            f"NO modelo {opportunity.model_probability_no:.1%} | YES {opportunity.market_yes_price:.3f} / NO {opportunity.market_no_price:.3f}"
        )
        lines.append(
            f"Comprar NO a {opportunity.limit_price:.3f} | edge {opportunity.edge:.1%} | elapsed {opportunity.elapsed_fraction:.1%}"
        )
        lines.append(market_url(opportunity))
        lines.append("")
    return "\n".join(lines).strip()


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    response = post_form_json(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        },
    )
    if not response.get("ok"):
        raise RuntimeError(f"Telegram devolvio una respuesta invalida: {response}")


def notify_opportunities(
    connection: sqlite3.Connection,
    opportunities: list[Opportunity],
    args: argparse.Namespace,
    logger: logging.Logger,
) -> int:
    if not args.telegram:
        return 0
    if not args.telegram_bot_token or not args.telegram_chat_id:
        logger.warning(
            "Telegram esta habilitado pero faltan POLYMARKET_TELEGRAM_BOT_TOKEN o POLYMARKET_TELEGRAM_CHAT_ID."
        )
        return 0

    eligible_opportunities: list[Opportunity] = []
    for opportunity in opportunities:
        if should_send_alert(connection, opportunity, args.notification_cooldown):
            eligible_opportunities.append(opportunity)
        if len(eligible_opportunities) >= args.notify_top:
            break

    if not eligible_opportunities:
        return 0

    send_telegram_message(
        bot_token=args.telegram_bot_token,
        chat_id=args.telegram_chat_id,
        text=build_telegram_digest(eligible_opportunities),
    )
    for opportunity in eligible_opportunities:
        record_sent_alert(connection, opportunity)
    return len(eligible_opportunities)


def execute_trades(
    connection: sqlite3.Connection,
    opportunities: list[Opportunity],
    args: argparse.Namespace,
    strategy: dict[str, Any],
    logger: logging.Logger,
) -> list[TradeExecution]:
    if not args.execute_trades:
        return []

    pending: list[Opportunity] = []
    max_trades = int(strategy.get("max_trades_per_cycle", DEFAULT_STRATEGY["max_trades_per_cycle"]))
    for opportunity in opportunities:
        if trade_already_recorded(connection, opportunity):
            continue
        if not opportunity.token_id:
            logger.warning(
                "Saltando %s porque Gamma no devolvio token_id para el lado %s.",
                opportunity.market_id,
                opportunity.recommended_side,
            )
            continue
        pending.append(opportunity)
        if len(pending) >= max_trades:
            break

    if not pending:
        return []

    client = build_trading_client(args)
    trades: list[TradeExecution] = []
    for opportunity in pending:
        try:
            response = place_limit_order(client, opportunity)
        except Exception as exc:
            logger.error(
                "No se pudo enviar orden para %s (%s): %s",
                opportunity.market_id,
                opportunity.recommended_side,
                exc,
            )
            continue
        trades.append(record_executed_trade(connection, opportunity, response))
    return trades


def build_plist_overrides(args: argparse.Namespace) -> list[str]:
    program_arguments: list[str] = []
    if args.telegram:
        program_arguments.append("--telegram")
    if args.execute_trades:
        program_arguments.append("--execute-trades")
    if args.keyword:
        for keyword in args.keyword:
            program_arguments.extend(["--keyword", keyword])

    optional_pairs = [
        ("--min-edge", args.min_edge),
        ("--min-confidence", args.min_confidence),
        ("--max-entry-price", args.max_entry_price),
        ("--entry-price-buffer", args.entry_price_buffer),
        ("--trade-amount-usdc", args.trade_amount_usdc),
        ("--max-trades-per-cycle", args.max_trades_per_cycle),
        ("--optimistic-yes-prior", args.optimistic_yes_prior),
        ("--yes-bias-buffer", args.yes_bias_buffer),
        ("--min-hours-remaining", args.min_hours_remaining),
        ("--max-hours-remaining", args.max_hours_remaining),
        ("--min-elapsed-fraction", args.min_elapsed_fraction),
        ("--min-market-duration-hours", args.min_market_duration_hours),
        ("--max-market-duration-hours", args.max_market_duration_hours),
    ]
    for flag, value in optional_pairs:
        if value is None:
            continue
        program_arguments.extend([flag, str(value)])
    return program_arguments


def write_launchd_plist(
    args: argparse.Namespace,
    python_executable: str,
    script_path: Path,
    db_path: Path,
    strategy_path: Path,
    telegram_env_path: Path,
    polymarket_env_path: Path,
    log_file_path: Path,
    plist_path: Path,
) -> Path:
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    program_arguments = [
        python_executable,
        str(script_path),
        "--service",
        "--telegram-env-file",
        str(telegram_env_path),
        "--polymarket-env-file",
        str(polymarket_env_path),
        "--interval",
        str(args.interval),
        "--top",
        str(args.top),
        "--limit-per-page",
        str(args.limit_per_page),
        "--db-path",
        str(db_path),
        "--strategy-file",
        str(strategy_path),
        "--log-file",
        str(log_file_path),
        "--notify-top",
        str(args.notify_top),
        "--notification-cooldown",
        str(args.notification_cooldown),
        "--min-liquidity",
        str(args.min_liquidity),
        "--min-volume-24h",
        str(args.min_volume_24h),
    ]
    program_arguments.extend(build_plist_overrides(args))

    plist_payload = {
        "Label": args.launchd_label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(script_path.parent),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(log_file_path.parent / "launchd.stdout.log"),
        "StandardErrorPath": str(log_file_path.parent / "launchd.stderr.log"),
    }

    with plist_path.open("wb") as handle:
        plistlib.dump(plist_payload, handle, sort_keys=False)
    return plist_path


def send_telegram_test_message(args: argparse.Namespace) -> int:
    if not args.telegram_bot_token or not args.telegram_chat_id:
        raise ValueError("Faltan credenciales de Telegram para enviar el mensaje de prueba.")
    send_telegram_message(args.telegram_bot_token, args.telegram_chat_id, args.telegram_test_message)
    return 0


def validate_strategy(strategy: dict[str, Any]) -> None:
    min_edge = safe_float(strategy.get("min_edge"), -1.0)
    if min_edge < 0:
        raise ValueError("min_edge no puede ser negativo.")
    min_confidence = safe_float(strategy.get("min_confidence"), -1.0)
    if min_confidence < 0 or min_confidence > 1:
        raise ValueError("min_confidence debe estar entre 0 y 1.")
    max_entry_price = safe_float(strategy.get("max_entry_price"), -1.0)
    if max_entry_price <= 0 or max_entry_price >= 1:
        raise ValueError("max_entry_price debe estar entre 0 y 1.")
    if safe_float(strategy.get("trade_amount_usdc"), 0.0) <= 0:
        raise ValueError("trade_amount_usdc debe ser mayor que 0.")
    if int(strategy.get("max_trades_per_cycle", 0)) <= 0:
        raise ValueError("max_trades_per_cycle debe ser mayor que 0.")
    optimistic_yes_prior = safe_float(strategy.get("optimistic_yes_prior"), -1.0)
    if optimistic_yes_prior <= 0 or optimistic_yes_prior >= 1:
        raise ValueError("optimistic_yes_prior debe estar entre 0 y 1.")
    if safe_float(strategy.get("yes_bias_buffer"), -1.0) < 0:
        raise ValueError("yes_bias_buffer no puede ser negativo.")
    min_hours_remaining = safe_float(strategy.get("min_hours_remaining"), -1.0)
    max_hours_remaining = safe_float(strategy.get("max_hours_remaining"), -1.0)
    if min_hours_remaining < 0:
        raise ValueError("min_hours_remaining no puede ser negativo.")
    if max_hours_remaining <= min_hours_remaining:
        raise ValueError("max_hours_remaining debe ser mayor que min_hours_remaining.")
    min_elapsed_fraction = safe_float(strategy.get("min_elapsed_fraction"), -1.0)
    if min_elapsed_fraction < 0 or min_elapsed_fraction >= 1:
        raise ValueError("min_elapsed_fraction debe estar entre 0 y 1, sin incluir 1.")
    min_market_duration_hours = safe_float(strategy.get("min_market_duration_hours"), -1.0)
    max_market_duration_hours = safe_float(strategy.get("max_market_duration_hours"), -1.0)
    if min_market_duration_hours < 0:
        raise ValueError("min_market_duration_hours no puede ser negativo.")
    if max_market_duration_hours <= min_market_duration_hours:
        raise ValueError("max_market_duration_hours debe ser mayor que min_market_duration_hours.")


def run_cycle(
    connection: sqlite3.Connection,
    args: argparse.Namespace,
    strategy: dict[str, Any],
    logger: logging.Logger,
) -> CycleResult:
    fetched_at = int(time.time())
    raw_markets = iter_active_markets(limit=args.limit_per_page, max_pages=args.max_pages)
    time_decay_markets = build_time_decay_markets(
        raw_markets=raw_markets,
        fetched_at=fetched_at,
        min_liquidity=args.min_liquidity,
        min_volume_24h=args.min_volume_24h,
        strategy=strategy,
    )

    opportunities: list[Opportunity] = []
    for market in time_decay_markets:
        opportunity = evaluate_market(market, strategy)
        if opportunity is not None:
            opportunities.append(opportunity)

    opportunities.sort(key=lambda item: item.edge, reverse=True)
    trades = execute_trades(connection, opportunities, args, strategy, logger)
    summary_text = render_opportunities(
        opportunities=opportunities,
        time_decay_market_count=len(time_decay_markets),
        trades=trades,
        fetched_at=fetched_at,
        top=args.top,
    )
    return CycleResult(
        fetched_at=fetched_at,
        time_decay_market_count=len(time_decay_markets),
        opportunities=opportunities,
        trades=trades,
        summary_text=summary_text,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.service:
        args.watch = True
    if args.interval < 5:
        parser.error("--interval debe ser de al menos 5 segundos para evitar demasiado ruido.")
    if args.limit_per_page < 1 or args.limit_per_page > 100:
        parser.error("--limit-per-page debe estar entre 1 y 100.")
    if args.notification_cooldown < 0:
        parser.error("--notification-cooldown no puede ser negativo.")
    if args.log_max_mb <= 0:
        parser.error("--log-max-mb debe ser mayor que 0.")
    if args.log_backups < 0:
        parser.error("--log-backups no puede ser negativo.")
    if args.min_liquidity < 0 or args.min_volume_24h < 0:
        parser.error("--min-liquidity y --min-volume-24h no pueden ser negativos.")

    script_path = Path(__file__).resolve()
    db_path = resolve_runtime_db_path(args.db_path)
    strategy_path = resolve_path(args.strategy_file)
    telegram_env_path = resolve_path(args.telegram_env_file)
    polymarket_env_path = resolve_path(args.polymarket_env_file)
    log_file_path = resolve_path(args.log_file)
    plist_path = resolve_path(args.launchd_plist_path)

    telegram_env_values = load_simple_env_file(telegram_env_path)
    polymarket_env_values = load_simple_env_file(polymarket_env_path)
    hydrate_telegram_credentials(args, {**polymarket_env_values, **telegram_env_values})
    hydrate_polymarket_credentials(args, polymarket_env_values)
    if args.polymarket_signature_type is None:
        args.polymarket_signature_type = 0

    logger = configure_logger(args, log_file_path)

    if args.write_launchd_plist:
        generated_path = write_launchd_plist(
            args=args,
            python_executable=sys.executable,
            script_path=script_path,
            db_path=db_path,
            strategy_path=strategy_path,
            telegram_env_path=telegram_env_path,
            polymarket_env_path=polymarket_env_path,
            log_file_path=log_file_path,
            plist_path=plist_path,
        )
        logger.info("Plist generado en %s", generated_path)
        return 0

    if args.telegram_test_message:
        try:
            send_telegram_test_message(args)
        except Exception as exc:
            logger.error("No se pudo enviar el mensaje de prueba a Telegram: %s", exc)
            return 1
        logger.info("Mensaje de prueba enviado a Telegram.")
        return 0

    strategy_path.parent.mkdir(parents=True, exist_ok=True)
    strategy = load_strategy(strategy_path, args)
    try:
        validate_strategy(strategy)
    except ValueError as exc:
        parser.error(str(exc))
    if args.execute_trades and not args.polymarket_private_key:
        parser.error("Hace falta POLYMARKET_PRIVATE_KEY para ejecutar órdenes reales.")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        ensure_db(connection)
        if not args.watch:
            result = run_cycle(connection, args, strategy, logger)
            logger.info(result.summary_text)
            sent_count = notify_opportunities(connection, result.opportunities, args, logger)
            if sent_count:
                logger.info("Alertas enviadas a Telegram: %s", sent_count)
            return 0

        iteration = 0
        while True:
            iteration += 1
            try:
                result = run_cycle(connection, args, strategy, logger)
                logger.info(result.summary_text)
                sent_count = notify_opportunities(connection, result.opportunities, args, logger)
                if sent_count:
                    logger.info("Alertas enviadas a Telegram: %s", sent_count)
            except KeyboardInterrupt:
                logger.error("Interrumpido por el usuario.")
                return 130
            except Exception as exc:
                logger.error("Error en iteración %s: %s", iteration, exc)

            if args.iterations and iteration >= args.iterations:
                break
            time.sleep(args.interval)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())