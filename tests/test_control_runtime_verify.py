from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "service.sh"


class ControlRuntimeVerifyTests(unittest.TestCase):
    def run_verify(self, config_text: str, model_info: dict) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config_path = temp / "config.yaml"
            model_info_path = temp / "model-info.json"
            config_path.write_text(textwrap.dedent(config_text).lstrip(), encoding="utf-8")
            model_info_path.write_text(json.dumps(model_info), encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "LITELLM_ALLOW_CHECKOUT_SERVICE": "1",
                    "LITELLM_RUNTIME_CONFIG": str(config_path),
                    "LITELLM_MODEL_INFO_FILE": str(model_info_path),
                    "PYTHON": sys.executable,
                }
            )
            return subprocess.run(
                ["/bin/bash", str(CONTROL), "verify-runtime-config"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_verify_runtime_config_accepts_matching_model_info(self) -> None:
        result = self.run_verify(
            """
            model_list:
              - model_name: default-chat
                litellm_params:
                  model: openai/default-chat
                  api_base: https://api.backup.example/v1
                  order: 2
                model_info:
                  id: a1b2c3d4
                  provider: backup_provider
                  route_key: backup_provider / openai/default-chat / order=2
            """,
            {
                "data": [
                    {
                        "model_name": "default-chat",
                        "litellm_params": {
                            "model": "openai/default-chat",
                            "api_base": "https://api.backup.example/v1",
                            "order": 2,
                        },
                        "model_info": {
                            "id": "a1b2c3d4",
                            "provider": "backup_provider",
                            "route_key": "backup_provider / openai/default-chat / order=2",
                        },
                    }
                ]
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Runtime routes verified: 1 deployments", result.stdout)

    def test_verify_runtime_config_rejects_disabled_compat_provider_still_in_runtime(self) -> None:
        result = self.run_verify(
            """
            model_list:
              - model_name: default-chat
                litellm_params:
                  model: openai/default-chat
                  api_base: https://api.backup.example/v1
                  order: 2
                model_info:
                  id: a1b2c3d4
                  provider: backup_provider
                  route_key: backup_provider / openai/default-chat / order=2
            """,
            {
                "data": [
                    {
                        "model_name": "default-chat",
                        "litellm_params": {
                            "model": "openai/default-chat",
                            "api_base": "https://api.backup.example/v1",
                            "order": 2,
                        },
                        "model_info": {
                            "id": "a1b2c3d4",
                            "provider": "backup_provider",
                            "route_key": "backup_provider / openai/default-chat / order=2",
                        },
                    },
                    {
                        "model_name": "default-chat",
                        "litellm_params": {
                            "model": "openai/default-chat",
                            "api_base": "https://headers.example/v1",
                            "order": 2,
                        },
                        "model_info": {
                            "id": "b2c3d4e5",
                            "provider": "compat_provider",
                            "route_key": "compat_provider / openai/default-chat / order=2",
                        },
                    },
                ]
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Runtime route mismatch", result.stderr)
        self.assertIn("Extra in runtime", result.stderr)
        self.assertIn("route=compat_provider / openai/default-chat / order=2", result.stderr)
        self.assertIn("token=b2c3d4e5", result.stderr)


if __name__ == "__main__":
    unittest.main()
