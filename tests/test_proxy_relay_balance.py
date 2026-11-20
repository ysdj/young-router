from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from young_router.core.runtime_settings_schema import RELAY_NEGATIVE_BALANCE_INTERVAL_KEY
from young_router.proxy import relay_balance


class RelayBalanceRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.config = root / ".litellm-runtime" / "config.yaml"
        self.accounts = root / ".litellm-runtime" / "relay-accounts.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(
            """
providers:
  relay:
    x-young-router-relay-keys:
      version: 1
      slots:
        - id: slot-negative
          api_key_name: key
          source:
            kind: relay
            station_id: station
            account_id: account-negative
            resource_id: resource
        - id: slot-positive
          api_key_name: positive
          source:
            kind: relay
            station_id: station
            account_id: account-positive
            resource_id: resource
""",
            encoding="utf-8",
        )
        self._set_env(**{
            "LITELLM_CONFIG_FILE": str(self.config),
            RELAY_NEGATIVE_BALANCE_INTERVAL_KEY: "5",
        })
        self.addCleanup(self._restore_env)

    def tearDown(self) -> None:
        relay_balance.clear_cache()
        self.tempdir.cleanup()

    def _set_env(self, **values: str) -> None:
        self.env = getattr(self, "env", {})
        for key, value in values.items():
            self.env.setdefault(key, os.environ.get(key))
            os.environ[key] = value

    def _restore_env(self) -> None:
        for key, value in getattr(self, "env", {}).items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write_accounts(self, *, negative_observed: str | None, positive: float = 1.0) -> None:
        self.accounts.write_text(
            json.dumps(
                {
                    "version": 1,
                    "accounts": [
                        {
                            "id": "account-negative",
                            "balance": -1.0,
                            "balance_observed_at": negative_observed,
                        },
                        {
                            "id": "account-positive",
                            "balance": positive,
                            "balance_observed_at": negative_observed,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        relay_balance.clear_cache()

    @staticmethod
    def _deployment(key_id: str) -> dict:
        return {
            "model_info": {
                "provider": "relay",
                "x-young-router-provider-key-id": key_id,
            }
        }

    def test_only_fresh_negative_balance_is_filtered(self) -> None:
        observed = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
        self._write_accounts(negative_observed=observed)
        negative = self._deployment("slot-negative")
        positive = self._deployment("slot-positive")
        ordinary = {"model_info": {"provider": "other"}}

        self.assertEqual(
            [positive, ordinary],
            relay_balance.filter_deployments([negative, positive, ordinary]),
        )

    def test_expired_or_missing_observation_keeps_route_eligible(self) -> None:
        expired = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat().replace("+00:00", "Z")
        self._write_accounts(negative_observed=expired)
        negative = self._deployment("slot-negative")
        self.assertEqual([negative], relay_balance.filter_deployments([negative]))

        self._write_accounts(negative_observed=None)
        self.assertEqual([negative], relay_balance.filter_deployments([negative]))

    def test_custom_interval_controls_negative_balance_expiry(self) -> None:
        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        negative = self._deployment("slot-negative")
        for interval, age, blocked in ((10, 7, True), (3, 4, False), (10, 10, False)):
            with self.subTest(interval=interval, age=age):
                self._set_env(**{RELAY_NEGATIVE_BALANCE_INTERVAL_KEY: str(interval)})
                self._write_accounts(negative_observed=(now - timedelta(minutes=age)).isoformat())
                with mock.patch.object(relay_balance, "datetime") as clock:
                    clock.now.return_value = now
                    self.assertEqual(
                        [] if blocked else [negative],
                        relay_balance.filter_deployments([negative]),
                    )

    def test_unreadable_documents_fail_open(self) -> None:
        self.accounts.write_text("not-json", encoding="utf-8")
        relay_balance.clear_cache()
        negative = self._deployment("slot-negative")
        self.assertEqual([negative], relay_balance.filter_deployments([negative]))

    def test_real_router_excludes_negative_balance_before_tier_selection(self) -> None:
        self._write_accounts(negative_observed=datetime.now(timezone.utc).isoformat())
        result = subprocess.run(
            [os.environ.get("LITELLM_TEST_PYTHON") or sys.executable, "-c", textwrap.dedent('''
                import asyncio
                from unittest.mock import patch
                import litellm
                from litellm import Router
                from litellm.types.router import RouterRateLimitError
                from young_router.proxy.patches import _install_relay_balance_selection_patch

                _install_relay_balance_selection_patch()
                router = Router(model_list=[
                    {
                        "model_name": "group",
                        "litellm_params": {"model": "openai/gpt-4o", "api_key": "sk-replace-example", "order": order},
                        "model_info": {"id": route, "provider": "relay", "x-young-router-provider-key-id": slot},
                    }
                    for order, route, slot in (
                        (0, "negative", "slot-negative"),
                        (1, "healthy", "slot-positive"),
                        (2, "excluded", "slot-positive"),
                    )
                ], num_retries=0)
                request = {
                    "messages": [{"role": "user", "content": "test"}],
                    "_excluded_deployment_ids": ["excluded"],
                    "disable_fallbacks": True,
                }

                def completion(**kwargs):
                    return litellm.ModelResponse(id=kwargs["model_info"]["id"])

                async def acompletion(**kwargs):
                    return completion(**kwargs)

                async def main():
                    with patch.object(litellm, "completion", completion), patch.object(litellm, "acompletion", acompletion):
                        assert router.completion(model="group", **request).id == "healthy"
                        assert (await router.acompletion(model="group", **request)).id == "healthy"
                        for call in (router.completion, router.acompletion):
                            for model in ("negative", "excluded"):
                                try:
                                    response = call(model=model, **request)
                                    if asyncio.iscoroutine(response):
                                        await response
                                except RouterRateLimitError:
                                    continue
                                raise AssertionError(f"{call.__name__} dispatched excluded ID {model}")

                asyncio.run(main())
                ''')],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
