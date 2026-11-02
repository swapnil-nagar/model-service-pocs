import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_live_poc import (
    AcceptanceProgress, LiveAcceptanceError, Tenant, deployment_target, execute, main,
    verify_owned_target, warm_runner,
)


SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"


class DriverTests(unittest.TestCase):
    def test_owned_runner_warmup_uses_only_configuration_gets_and_can_recover_cold_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deployment.json"
            path.write_text(json.dumps({
                "runnerFqdn": "fixture.azurecontainerapps.io", "image": "fixture.azurecr.io/poc:tag",
            }), encoding="utf-8")
            with patch("run_live_poc.az_json", return_value=[
                {"fqdn": "fixture.azurecontainerapps.io", "image": "fixture.azurecr.io/poc:tag"},
            ]), patch("run_live_poc.call", side_effect=[
                LiveAcceptanceError("gateway_request_failed_or_timeout"),
                (200, {"status": "configured", "mode": "foundry", "live_enabled": True}),
            ]) as request, patch("run_live_poc.time.sleep"), patch("run_live_poc.vault_secret") as secret:
                warm_runner(path, "fixture-group", SUBSCRIPTION)
                self.assertEqual(request.call_count, 2)
                for entry in request.call_args_list:
                    self.assertEqual(entry.args, ("https://fixture.azurecontainerapps.io", "/healthz"))
                    self.assertIsNone(entry.kwargs["tenant"])
                secret.assert_not_called()

    def test_unowned_runner_is_rejected_before_any_health_or_model_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deployment.json"
            path.write_text(json.dumps({
                "runnerFqdn": "fixture.azurecontainerapps.io", "image": "fixture.azurecr.io/poc:tag",
            }), encoding="utf-8")
            with patch("run_live_poc.az_json", return_value=[]), patch("run_live_poc.call") as request:
                with self.assertRaisesRegex(LiveAcceptanceError, "target_not_owned"):
                    warm_runner(path, "fixture-group", SUBSCRIPTION)
                request.assert_not_called()

    def test_failed_negative_check_records_zero_turn_attempts_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            progress_file = Path(directory) / "progress.json"
            progress = AcceptanceProgress(progress_file)
            with patch("run_live_poc.call", side_effect=[
                (401, {}), (401, {}), (403, {}), (502, {}),
            ]), patch("run_live_poc.run_turn") as turn:
                with self.assertRaisesRegex(LiveAcceptanceError, "negative_authorization_checks_failed"):
                    execute(
                        "https://fixture.azure-api.net",
                        Tenant("customer-a", "SYNTHETIC_KEY_A"), Tenant("customer-b", "SYNTHETIC_KEY_B"),
                        progress,
                    )
                turn.assert_not_called()
            text = progress_file.read_text(encoding="utf-8")
            self.assertNotIn("SYNTHETIC_KEY", text)
            self.assertEqual(progress.turn_submit_attempts, 0)
            self.assertFalse(json.loads(text)["checks"]["unapproved_model_rejected"])

    def test_failed_model_turn_is_checkpointed_before_submission_and_not_retried(self):
        progress = AcceptanceProgress()
        with patch("run_live_poc.call", side_effect=[
            (401, {}), (401, {}), (403, {}), (403, {}),
        ]), patch(
            "run_live_poc.run_turn", side_effect=LiveAcceptanceError("live_run_failed"),
        ) as turn:
            with self.assertRaisesRegex(LiveAcceptanceError, "live_run_failed"):
                execute(
                    "https://fixture.azure-api.net",
                    Tenant("customer-a", "SYNTHETIC_KEY_A"), Tenant("customer-b", "SYNTHETIC_KEY_B"),
                    progress,
                )
            self.assertEqual(turn.call_count, 1)
        self.assertEqual(progress.turn_submit_attempts, 1)
        self.assertEqual(progress.accepted_turns, 0)
        self.assertEqual(progress.stage, "customer-a_setup_submission")

    def test_paid_flag_is_required_before_loading_targets_or_credentials(self):
        argv = [
            "run_live_poc.py", "--deployment-file", "not-read.json",
            "--subscription", SUBSCRIPTION,
        ]
        with patch("sys.argv", argv), patch("run_live_poc.vault_secret") as secret:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                main()
            self.assertEqual(result.exception.code, 2)
            secret.assert_not_called()

    def test_target_must_match_successful_deployment_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deployment.json"
            path.write_text(json.dumps({
                "status": "configuration_healthy",
                "subscriptionId": SUBSCRIPTION,
                "resourceGroup": "fixture-group",
                "keyVaultName": "fixture-vault",
                "gatewayUrl": "https://fixture.azure-api.net",
            }), encoding="utf-8")
            self.assertEqual(
                deployment_target(path, SUBSCRIPTION),
                ("https://fixture.azure-api.net", "fixture-vault", "fixture-group"),
            )
            with self.assertRaises(RuntimeError):
                deployment_target(path, "22222222-2222-2222-2222-222222222222")

    def test_gateway_and_credential_store_are_checked_before_keys_are_sent(self):
        with patch("run_live_poc.az_json", side_effect=[
            "https://other.azure-api.net", "fixture-vault",
        ]), self.assertRaisesRegex(RuntimeError, "owned deployment"):
            verify_owned_target(
                "https://fixture.azure-api.net", "fixture-vault", "fixture-group", SUBSCRIPTION
            )

    def test_failure_overwrites_stale_success_without_publishing_error_content(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            output.write_text('{"status":"passed"}', encoding="utf-8")
            argv = [
                "run_live_poc.py", "--deployment-file", str(Path(directory) / "missing.json"),
                "--subscription", SUBSCRIPTION, "--allow-paid-model-calls",
                "--output", str(output),
            ]
            with patch("sys.argv", argv), patch("run_live_poc.vault_secret") as secret:
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    main()
                secret.assert_not_called()
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["error_type"], "FileNotFoundError")
            self.assertIsNone(data["successful_inference_calls"])
            self.assertNotIn(directory, output.read_text(encoding="utf-8"))
