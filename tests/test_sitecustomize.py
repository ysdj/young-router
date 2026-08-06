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


class SiteCustomizeTests(unittest.TestCase):
    def python(self) -> str:
        helper = ROOT / ".venv/bin/python"
        return str(helper if helper.exists() else sys.executable)

    def require_litellm(self) -> None:
        result = subprocess.run(
            [self.python(), "-c", "import litellm"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("litellm is not installed for this Python")

    def run_probe(
        self,
        *,
        runtime: Path,
        template: Path,
        pythonpath_extra: list[Path],
        code: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("LITELLM_MENU_PROXY_PROCESS", None)
        env.pop("LITELLM_MENU_TIMESTAMP_OUTPUT", None)
        env.update(
            {
                "LITELLM_TEMPLATE_ROOT": str(template),
                "PYTHONPATH": os.pathsep.join(str(path) for path in [*pythonpath_extra, ROOT]),
            }
        )
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [self.python(), "-c", code],
            cwd=runtime,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_litellm_menu_config_callback_imports_from_pythonpath(self) -> None:
        self.require_litellm()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime = temp / "runtime"
            template = temp / "template"
            runtime.mkdir()
            package = template / "litellm_menu"
            package.mkdir(parents=True)
            (runtime / "config.yaml").write_text("model_list: []\n", encoding="utf-8")
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "callbacks.py").write_text(
                "value = 'loaded-from-template'\n",
                encoding="utf-8",
            )

            result = self.run_probe(
                runtime=runtime,
                template=template,
                pythonpath_extra=[template],
                code=textwrap.dedent(
                    """
                    from litellm.proxy.types_utils.utils import get_instance_fn
                    print(get_instance_fn("litellm_menu.callbacks.value", config_file_path="config.yaml"))
                    """
                ),
                extra_env={"LITELLM_MENU_PROXY_PROCESS": "1"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "loaded-from-template")
            self.assertFalse((runtime / "litellm_menu" / "callbacks.py").exists())

    def test_litellm_config_callback_fallback_rejects_unowned_modules(self) -> None:
        self.require_litellm()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime = temp / "runtime"
            template = temp / "template"
            runtime.mkdir()
            template.mkdir()
            (runtime / "config.yaml").write_text("model_list: []\n", encoding="utf-8")
            (template / "allowed_hook.py").write_text(
                "value = 'loaded-from-template'\n",
                encoding="utf-8",
            )

            result = self.run_probe(
                runtime=runtime,
                template=template,
                pythonpath_extra=[template],
                code=textwrap.dedent(
                    """
                    from litellm.proxy.types_utils.utils import get_instance_fn
                    try:
                        get_instance_fn("allowed_hook.value", config_file_path="config.yaml")
                    except ImportError as exc:
                        print(type(exc).__name__)
                    else:
                        raise SystemExit("expected ImportError")
                    """
                ),
                extra_env={"LITELLM_MENU_PROXY_PROCESS": "1"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "ImportError")

    def test_openai_image_edit_accepts_chat_style_usage(self) -> None:
        self.require_litellm()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime = temp / "runtime"
            runtime.mkdir()
            result = self.run_probe(
                runtime=runtime,
                template=ROOT,
                pythonpath_extra=[],
                code=textwrap.dedent(
                    """
                    import httpx
                    from litellm.llms.openai.image_edit.transformation import (
                        OpenAIImageEditConfig,
                    )
                    from sitecustomize import _IMAGE_EDIT_USAGE_PATCH_ATTR

                    transform = OpenAIImageEditConfig.transform_image_edit_response
                    assert getattr(transform, _IMAGE_EDIT_USAGE_PATCH_ATTR)

                    raw_response = httpx.Response(
                        200,
                        json={
                            "created": 1780000000,
                            "data": [{"b64_json": "abc"}],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1000,
                                "total_tokens": 1001,
                            },
                        },
                    )
                    response = OpenAIImageEditConfig().transform_image_edit_response(
                        "gpt-image-2",
                        raw_response,
                        None,
                    )
                    print(
                        response.usage.input_tokens,
                        response.usage.output_tokens,
                        response.usage.total_tokens,
                        response.usage.input_tokens_details.image_tokens,
                        response.usage.input_tokens_details.text_tokens,
                    )
                    """
                ),
                extra_env={"LITELLM_MENU_PROXY_PROCESS": "1"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "1 1000 1001 0 0")

    def test_non_proxy_python_does_not_import_litellm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "runtime"
            runtime.mkdir()

            result = self.run_probe(
                runtime=runtime,
                template=ROOT,
                pythonpath_extra=[],
                code=textwrap.dedent(
                    """
                    import sys

                    print(any(name == "litellm" or name.startswith("litellm.") for name in sys.modules))
                    """
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "False")

    def test_proxy_sitecustomize_defers_litellm_import(self) -> None:
        self.require_litellm()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "runtime"
            runtime.mkdir()

            result = self.run_probe(
                runtime=runtime,
                template=ROOT,
                pythonpath_extra=[],
                code=textwrap.dedent(
                    """
                    import sys

                    print(any(name == "litellm" or name.startswith("litellm.") for name in sys.modules))
                    """
                ),
                extra_env={"LITELLM_MENU_PROXY_PROCESS": "1"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "False")

    def test_proxy_console_lines_receive_utc_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "runtime"
            runtime.mkdir()

            result = self.run_probe(
                runtime=runtime,
                template=ROOT,
                pythonpath_extra=[],
                code=textwrap.dedent(
                    """
                    import sys

                    print("service ready")
                    print("worker ready", file=sys.stderr)
                    """
                ),
                extra_env={
                    "LITELLM_MENU_PROXY_PROCESS": "1",
                    "LITELLM_MENU_TIMESTAMP_OUTPUT": "1",
                },
            )

            self.assertEqual(result.returncode, 0)
            self.assertRegex(result.stdout, r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\] service ready\n$")
            self.assertRegex(result.stderr, r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\] worker ready\n$")

    def test_proxy_console_log_rotates_current_file_and_keeps_previous_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime = temp / "runtime"
            runtime.mkdir()
            service_log = temp / "menu-server.log"
            service_log.write_text("a" * 300_000, encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "LITELLM_MENU_PROXY_PROCESS": "1",
                    "LITELLM_MENU_TIMESTAMP_OUTPUT": "1",
                    "LITELLM_MENU_LOG_MAX_BYTES": "262144",
                    "LITELLM_MENU_SERVICE_LOG": str(service_log),
                    "PYTHONPATH": str(ROOT),
                }
            )
            with service_log.open("a", encoding="utf-8") as output:
                result = subprocess.run(
                    [self.python(), "-c", 'print("latest service line")'],
                    cwd=runtime,
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    check=False,
                )

            self.assertEqual(result.returncode, 0)
            self.assertIn("latest service line", service_log.read_text(encoding="utf-8"))
            self.assertLess(service_log.stat().st_size, 1024)
            backup = Path(f"{service_log}.1")
            self.assertTrue(backup.exists())
            self.assertEqual(backup.stat().st_size, 262144)

    def test_proxy_optional_database_driver_does_not_break_auth_error_classification(self) -> None:
        self.require_litellm()
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "runtime"
            runtime.mkdir()

            result = self.run_probe(
                runtime=runtime,
                template=ROOT,
                pythonpath_extra=[],
                code=textwrap.dedent(
                    """
                    from litellm.proxy.db.exception_handler import PrismaDBExceptionHandler
                    from sitecustomize import _OPTIONAL_DATABASE_ERROR_PATCH_ATTR

                    classifier = PrismaDBExceptionHandler.is_database_service_unavailable_error
                    assert getattr(classifier, _OPTIONAL_DATABASE_ERROR_PATCH_ATTR)
                    print(classifier(Exception("No api key passed in.")))
                    """
                ),
                extra_env={"LITELLM_MENU_PROXY_PROCESS": "1"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "False")

    def test_disable_system_proxy_lookup_uses_environment_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime = temp / "runtime"
            runtime.mkdir()
            result = self.run_probe(
                runtime=runtime,
                template=ROOT,
                pythonpath_extra=[],
                code=textwrap.dedent(
                    """
                    import os
                    import urllib.request
                    from sitecustomize import _SYSTEM_PROXY_LOOKUP_PATCH_ATTR

                    assert getattr(urllib.request.getproxies, _SYSTEM_PROXY_LOOKUP_PATCH_ATTR)
                    assert getattr(urllib.request.proxy_bypass, _SYSTEM_PROXY_LOOKUP_PATCH_ATTR)
                    os.environ.pop("HTTP_PROXY", None)
                    os.environ.pop("http_proxy", None)
                    print(urllib.request.getproxies())
                    """
                ),
                extra_env={"LITELLM_MENU_DISABLE_SYSTEM_PROXY_LOOKUP": "1"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "{}")

    @unittest.skipUnless(sys.platform == "darwin", "macOS SystemConfiguration behavior")
    def test_cached_macos_proxy_lookup_never_reenters_system_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime = temp / "runtime"
            runtime.mkdir()
            snapshot = json.dumps(
                {
                    "source": "macos",
                    "proxies": {"https": "http://system-proxy.example:8080"},
                    "settings": {
                        "exclude_simple": True,
                        "exceptions": ["*.example.test"],
                    },
                }
            )
            result = self.run_probe(
                runtime=runtime,
                template=ROOT,
                pythonpath_extra=[],
                code=textwrap.dedent(
                    """
                    import os
                    import urllib.request
                    from sitecustomize import _SYSTEM_PROXY_LOOKUP_PATCH_ATTR

                    assert getattr(urllib.request.getproxies, _SYSTEM_PROXY_LOOKUP_PATCH_ATTR)
                    assert getattr(urllib.request.proxy_bypass, _SYSTEM_PROXY_LOOKUP_PATCH_ATTR)
                    assert "LITELLM_MENU_SYSTEM_PROXY_SNAPSHOT" not in os.environ
                    print(urllib.request.getproxies())
                    print(urllib.request.proxy_bypass("printer"))
                    print(urllib.request.proxy_bypass("service.example.test"))
                    print(urllib.request.proxy_bypass("public.test"))
                    """
                ),
                extra_env={"LITELLM_MENU_SYSTEM_PROXY_SNAPSHOT": snapshot},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                [
                    "{'https': 'http://system-proxy.example:8080'}",
                    "True",
                    "True",
                    "False",
                ],
                result.stdout.strip().splitlines(),
            )


if __name__ == "__main__":
    unittest.main()
