from __future__ import annotations

import argparse
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch
from urllib.error import HTTPError

from polymarket_time_decay_bot import DEFAULT_STRATEGY
from polymarket_time_decay_bot import Opportunity
from polymarket_time_decay_bot import TimeDecayMarket
from polymarket_time_decay_bot import build_time_decay_markets
from polymarket_time_decay_bot import build_telegram_digest
from polymarket_time_decay_bot import calculate_time_decay_yes_probability
from polymarket_time_decay_bot import ensure_db
from polymarket_time_decay_bot import evaluate_market
from polymarket_time_decay_bot import extract_time_decay_market
from polymarket_time_decay_bot import fetch_json
from polymarket_time_decay_bot import hydrate_polymarket_credentials
from polymarket_time_decay_bot import hydrate_telegram_credentials
from polymarket_time_decay_bot import load_simple_env_file
from polymarket_time_decay_bot import parse_yes_no_prices
from polymarket_time_decay_bot import run_cycle
from polymarket_time_decay_bot import send_telegram_message


def build_raw_market(
    *,
    market_id: str = "m-1",
    question: str = "Will Apple announce an AR device today?",
    yes_price: str = "0.18",
    no_price: str = "0.82",
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    description: str = "Resolves YES if Apple announces an AR device before market close.",
    liquidity: float = 12_000.0,
    volume_24h: float = 5_000.0,
) -> dict[str, object]:
    if start_time is None:
        start_time = datetime(2026, 5, 17, 0, 0, tzinfo=timezone.utc)
    if end_time is None:
        end_time = datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc)
    return {
        "id": market_id,
        "question": question,
        "slug": f"slug-{market_id}",
        "conditionId": f"cond-{market_id}",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": json_string([yes_price, no_price]),
        "clobTokenIds": '["yes-token", "no-token"]',
        "orderPriceMinTickSize": "0.01",
        "negRisk": False,
        "liquidityNum": liquidity,
        "volume24hr": volume_24h,
        "feeType": "prediction",
        "description": description,
        "startDate": start_time.isoformat().replace("+00:00", "Z"),
        "endDate": end_time.isoformat().replace("+00:00", "Z"),
        "events": [
            {
                "id": f"event-{market_id}",
                "title": question,
                "slug": f"event-{market_id}",
                "description": description,
                "startDate": start_time.isoformat().replace("+00:00", "Z"),
                "endDate": end_time.isoformat().replace("+00:00", "Z"),
            }
        ],
    }


def json_string(values: list[str]) -> str:
    return "[" + ", ".join(f'\"{value}\"' for value in values) + "]"


class ParsePricesAndEnvTests(unittest.TestCase):
    def test_parse_yes_no_prices(self) -> None:
        market = {
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.61", "0.39"]',
        }
        self.assertEqual(parse_yes_no_prices(market), (0.61, 0.39))

    def test_non_binary_market_is_ignored(self) -> None:
        market = {
            "outcomes": '["A", "B"]',
            "outcomePrices": '["0.61", "0.39"]',
        }
        self.assertIsNone(parse_yes_no_prices(market))

    def test_load_simple_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / "telegram.env"
            env_file.write_text(
                "POLYMARKET_TELEGRAM_BOT_TOKEN=test-token\n"
                "POLYMARKET_TELEGRAM_CHAT_ID=12345\n",
                encoding="utf-8",
            )

            values = load_simple_env_file(env_file)

        self.assertEqual(values["POLYMARKET_TELEGRAM_BOT_TOKEN"], "test-token")
        self.assertEqual(values["POLYMARKET_TELEGRAM_CHAT_ID"], "12345")

    def test_hydrate_telegram_credentials(self) -> None:
        args = argparse.Namespace(telegram_bot_token="", telegram_chat_id="")

        hydrate_telegram_credentials(
            args,
            {
                "POLYMARKET_TELEGRAM_BOT_TOKEN": "token-from-file",
                "POLYMARKET_TELEGRAM_CHAT_ID": "chat-from-file",
            },
        )

        self.assertEqual(args.telegram_bot_token, "token-from-file")
        self.assertEqual(args.telegram_chat_id, "chat-from-file")

    def test_hydrate_polymarket_credentials(self) -> None:
        args = argparse.Namespace(
            execute_trades=False,
            polymarket_private_key="",
            polymarket_funder_address="",
            polymarket_signature_type=None,
            polymarket_clob_api_key="",
            polymarket_clob_api_secret="",
            polymarket_clob_api_passphrase="",
        )

        hydrate_polymarket_credentials(
            args,
            {
                "POLYMARKET_PRIVATE_KEY": "pk",
                "POLYMARKET_FUNDER_ADDRESS": "0xfunder",
                "POLYMARKET_SIGNATURE_TYPE": "3",
                "POLYMARKET_CLOB_API_KEY": "api-key",
                "POLYMARKET_CLOB_API_SECRET": "api-secret",
                "POLYMARKET_CLOB_API_PASSPHRASE": "api-pass",
                "POLYMARKET_EXECUTE_TRADES": "true",
            },
        )

        self.assertTrue(args.execute_trades)
        self.assertEqual(args.polymarket_private_key, "pk")
        self.assertEqual(args.polymarket_funder_address, "0xfunder")
        self.assertEqual(args.polymarket_signature_type, 3)
        self.assertEqual(args.polymarket_clob_api_key, "api-key")


class HttpRetryTests(unittest.TestCase):
    def test_fetch_json_retries_after_http_429(self) -> None:
        headers = Message()
        headers["Retry-After"] = "7"
        rate_limited_error = HTTPError(
            url="https://example.com",
            code=429,
            msg="Too Many Requests",
            hdrs=headers,
            fp=None,
        )
        successful_response = MagicMock()
        successful_response.__enter__.return_value = io.StringIO('{"ok": true}')
        successful_response.__exit__.return_value = False

        with patch("polymarket_time_decay_bot.urlopen", side_effect=[rate_limited_error, successful_response]) as mock_urlopen:
            with patch("polymarket_time_decay_bot.time.sleep") as mock_sleep:
                payload = fetch_json("https://example.com")

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(7.0)


class TelegramDigestTests(unittest.TestCase):
    def test_build_telegram_digest_uses_human_readable_labels(self) -> None:
        opportunity = Opportunity(
            event_id="event-1",
            event_title="Apple AR",
            event_slug="apple-ar",
            market_id="m-1",
            question="Will Apple announce an AR device today?",
            slug="apple-ar-device",
            start_time=datetime(2026, 5, 17, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc),
            window_label="daily",
            hours_remaining=3.5,
            total_hours=24.0,
            elapsed_fraction=0.854,
            baseline_yes_probability=0.5,
            model_probability_yes=0.18,
            model_probability_no=0.82,
            market_yes_price=0.24,
            market_no_price=0.76,
            recommended_side="NO",
            current_side_price=0.76,
            confidence=0.91,
            edge=0.06,
            limit_price=0.79,
            share_size=12.5,
            token_id="no-token",
            tick_size="0.01",
            neg_risk=False,
            liquidity=12_000.0,
            volume_24h=5_400.0,
            fetched_at=int(datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc).timestamp()),
        )

        digest = build_telegram_digest([opportunity])

        self.assertEqual(digest.splitlines()[0], "<b>Polymarket Time Decay Bot</b>")
        self.assertIn("<b>Actualizado:</b> <code>2026-05-17 12:00:00 UTC</code>", digest)
        self.assertIn("<b>1. Will Apple announce an AR device today?</b>", digest)
        self.assertIn("<b>Ventana:</b> daily | <b>Cierre estimado:</b> 3.5h", digest)
        self.assertIn("<b>Modelo:</b> YES 18.0% | NO 82.0%", digest)
        self.assertIn("<b>Mercado:</b> YES 0.240 | NO 0.760", digest)
        self.assertIn("<b>Accion sugerida:</b> Comprar <b>NO</b> a <code>0.790</code>", digest)
        self.assertIn("<b>Ventaja estimada:</b> 6.0% | <b>Tiempo transcurrido:</b> 85.4%", digest)
        self.assertIn("<b>Liquidez:</b> $12.0K | <b>Vol 24h:</b> $5.4K", digest)
        self.assertIn('<a href="https://polymarket.com/event/apple-ar">Abrir mercado</a>', digest)

    def test_send_telegram_message_uses_html_parse_mode(self) -> None:
        with patch("polymarket_time_decay_bot.post_form_json", return_value={"ok": True}) as mock_post:
            send_telegram_message("bot-token", "chat-id", "<b>hola</b>")

        self.assertEqual(mock_post.call_count, 1)
        payload = mock_post.call_args.args[1]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(payload["text"], "<b>hola</b>")


class TimeDecayParsingTests(unittest.TestCase):
    def test_extract_time_decay_market_parses_binary_market(self) -> None:
        fetched_at = int(datetime(2026, 5, 17, 12, tzinfo=timezone.utc).timestamp())
        raw_market = build_raw_market()

        market = extract_time_decay_market(raw_market, fetched_at)

        self.assertIsNotNone(market)
        assert market is not None
        self.assertEqual(market.market_id, "m-1")
        self.assertEqual(market.yes_token_id, "yes-token")
        self.assertEqual(market.no_token_id, "no-token")
        self.assertEqual(market.window_label, "daily")

    def test_build_time_decay_markets_filters_blocked_keywords_and_duration(self) -> None:
        fetched_at = int(datetime(2026, 5, 17, 12, tzinfo=timezone.utc).timestamp())
        long_market = build_raw_market(
            market_id="m-long",
            question="Will Apple announce an AR device before next quarter ends?",
            start_time=datetime(2026, 5, 1, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 8, 1, 0, tzinfo=timezone.utc),
        )
        sports_market = build_raw_market(
            market_id="m-sports",
            question="Will Team A score 3 goals today?",
            description="Resolves YES if Team A scores 3 goals today.",
        )
        valid_market = build_raw_market(market_id="m-valid")

        strategy = dict(DEFAULT_STRATEGY)
        strategy["required_keywords"] = ["announce"]
        strategy["blocked_keywords"] = ["goal", "goals"]
        strategy["keyword_filters"] = []

        markets = build_time_decay_markets(
            raw_markets=[long_market, sports_market, valid_market],
            fetched_at=fetched_at,
            min_liquidity=0.0,
            min_volume_24h=0.0,
            strategy=strategy,
        )

        self.assertEqual([market.market_id for market in markets], ["m-valid"])


class OpportunityTests(unittest.TestCase):
    def build_market(self, *, fetched_at: int, yes_price: float = 0.18, no_price: float = 0.82) -> TimeDecayMarket:
        start_time = datetime(2026, 5, 17, 0, 0, tzinfo=timezone.utc)
        end_time = datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc)
        return TimeDecayMarket(
            market_id="m-1",
            event_id="event-1",
            event_title="Will Apple announce an AR device today?",
            event_slug="apple-ar-device",
            question="Will Apple announce an AR device today?",
            slug="apple-ar-device-m1",
            condition_id="cond-1",
            yes_price=yes_price,
            no_price=no_price,
            yes_token_id="yes-token",
            no_token_id="no-token",
            liquidity=12_000.0,
            volume_24h=5_000.0,
            fee_type="prediction",
            tick_size="0.01",
            neg_risk=False,
            description="",
            start_time=start_time,
            end_time=end_time,
            total_hours=24.0,
            window_label="daily",
            search_text="will apple announce an ar device today",
            fetched_at=fetched_at,
        )

    def test_calculate_time_decay_yes_probability_shrinks_late_in_window(self) -> None:
        probability = calculate_time_decay_yes_probability(0.65, remaining_fraction=2.0 / 24.0)
        self.assertLess(probability, 0.10)

    def test_evaluate_market_recommends_no_when_time_is_running_out(self) -> None:
        fetched_at = int(datetime(2026, 5, 17, 22, 0, tzinfo=timezone.utc).timestamp())
        market = self.build_market(fetched_at=fetched_at)
        strategy = dict(DEFAULT_STRATEGY)

        opportunity = evaluate_market(market, strategy)

        self.assertIsNotNone(opportunity)
        assert opportunity is not None
        self.assertEqual(opportunity.recommended_side, "NO")
        self.assertEqual(opportunity.token_id, "no-token")
        self.assertGreater(opportunity.edge, 0.08)
        self.assertGreater(opportunity.model_probability_no, 0.90)

    def test_evaluate_market_skips_market_too_early_in_window(self) -> None:
        fetched_at = int(datetime(2026, 5, 17, 6, 0, tzinfo=timezone.utc).timestamp())
        market = self.build_market(fetched_at=fetched_at)
        strategy = dict(DEFAULT_STRATEGY)

        opportunity = evaluate_market(market, strategy)

        self.assertIsNone(opportunity)


class RunCycleTests(unittest.TestCase):
    def test_run_cycle_finds_time_decay_opportunity(self) -> None:
        fetched_at = int(datetime(2026, 5, 17, 22, 0, tzinfo=timezone.utc).timestamp())
        raw_markets = [
            build_raw_market(
                market_id="m-1",
                question="Will Apple announce an AR device today?",
                start_time=datetime(2026, 5, 17, 0, 0, tzinfo=timezone.utc),
                end_time=datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc),
            ),
            build_raw_market(
                market_id="m-2",
                question="Will Team A score 3 goals today?",
                description="Resolves YES if Team A scores 3 goals today.",
                start_time=datetime(2026, 5, 17, 0, 0, tzinfo=timezone.utc),
                end_time=datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc),
            ),
        ]

        strategy = dict(DEFAULT_STRATEGY)
        strategy["required_keywords"] = ["announce"]
        strategy["blocked_keywords"] = ["goal", "goals"]
        args = argparse.Namespace(
            limit_per_page=100,
            max_pages=0,
            min_liquidity=0.0,
            min_volume_24h=0.0,
            top=10,
            execute_trades=False,
        )

        connection = sqlite3.connect(":memory:")
        try:
            ensure_db(connection)
            with patch("polymarket_time_decay_bot.iter_active_markets", return_value=raw_markets):
                with patch("polymarket_time_decay_bot.time.time", return_value=float(fetched_at)):
                    result = run_cycle(connection, args, strategy, MagicMock())
        finally:
            connection.close()

        self.assertEqual(result.time_decay_market_count, 1)
        self.assertEqual(len(result.opportunities), 1)
        self.assertIn("time_decay", result.summary_text)
        self.assertEqual(result.opportunities[0].market_id, "m-1")


if __name__ == "__main__":
    unittest.main()