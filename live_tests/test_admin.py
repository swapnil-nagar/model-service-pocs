import json
import os
import re
import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.cloud_cli import CloudCommandError, audit_commands, az_json, az_output
from scripts.deploy_live_pocs import (
    ROOT, SECRET_PARAMETERS, completed_deployment, deploy, parameters, preview, write_private_parameters,
)
from scripts.private_checkpoint import load, save


def plan():
    types = (
        "resourceGroup", "foundryAccount", "modelDeployment", "apiManagement",
        "containerEnvironment", "foundryRunner", "containerRegistry", "keyVault",
        "logAnalytics", "applicationInsights", "foundryIdentity",
    )
    return {
        "naming": {
            "resourcePrefix": "fixture-poc",
            "suffix": "fixture",
            "resources": [{"type": kind, "name": "fixture-" + kind} for kind in types],
        },
        "deploymentVariables": {"location": "eastus2", "sessionId": "fixture-session"},
        "pocDesign": {
            "tenantASubscriptionId": "fixture-a",
            "tenantBSubscriptionId": "fixture-b",
            "modelName": "gpt-4.1-mini",
            "modelVersion": "2025-04-14",
            "modelCapacity": 3,
        },
    }


class AdminTests(unittest.TestCase):
    def test_portable_lock_accepts_only_sha256_hashes_for_every_pin(self):
        source = (ROOT / "requirements-live.lock").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r"--hash=([^:]+):", source)), {"sha256"})
        for block in re.split(r"(?m)(?=^[A-Za-z0-9._-]+==)", source):
            if block.strip():
                self.assertRegex(block, r"--hash=sha256:[a-f0-9]{64}")

    def test_completed_foundation_reuses_only_matching_template_and_parameters(self):
        expected = {"parameters": {
            "resourceGroupName": {"value": "fixture-group"},
            "createdAt": {"value": "new-time"},
            "foundryRunnerBackendKey": {"value": "SYNTHETIC_CREDENTIAL"},
        }}
        deployed = {
            "provisioningState": "Succeeded", "templateHash": "fixture-hash",
            "parameters": {
                "resourceGroupName": {"value": "fixture-group"},
                "createdAt": {"value": "original-time"},
                "foundryRunnerBackendKey": {"type": "SecureString"},
            },
            "outputs": {"resourceGroupName": {"value": "fixture-group"}},
        }
        compiled = {"metadata": {"_generator": {"templateHash": "fixture-hash"}}}
        with patch("scripts.deploy_live_pocs.az_json", side_effect=[deployed, compiled]):
            self.assertEqual(
                completed_deployment("fixture", Path("main.bicep"), expected, "fixture", ()),
                (deployed["outputs"], "original-time"),
            )
        for changed in (
            {**deployed, "templateHash": "old-hash"},
            {**deployed, "parameters": {
                **deployed["parameters"], "resourceGroupName": {"value": "different-group"},
            }},
        ):
            with self.subTest(changed=changed), patch(
                "scripts.deploy_live_pocs.az_json", side_effect=[changed, compiled],
            ):
                self.assertIsNone(completed_deployment("fixture", Path("main.bicep"), expected, "fixture", ()))
        with patch("scripts.deploy_live_pocs.az_json", return_value=deployed) as command:
            self.assertEqual(
                completed_deployment("fixture", Path("main.bicep"), expected, "fixture", (), match_template=False),
                (deployed["outputs"], "original-time"),
            )
            self.assertEqual(command.call_count, 1)

    def test_running_foundation_is_not_overlapped_or_treated_as_complete(self):
        with patch("scripts.deploy_live_pocs.az_json", return_value={"provisioningState": "Running"}):
            with self.assertRaisesRegex(RuntimeError, "not terminal"):
                completed_deployment("fixture", Path("main.bicep"), {}, "fixture", ())

    def test_image_reuse_requires_explicit_resume_before_any_cloud_call(self):
        with patch("scripts.deploy_live_pocs.az_json") as command:
            with self.assertRaisesRegex(RuntimeError, "requires --resume"):
                deploy(Path("not-read.json"), "fixture", resume=False, reuse_image=True)
            command.assert_not_called()

    def test_verified_image_reuse_skips_build_and_rejects_live_image_drift(self):
        subscription = "11111111-1111-1111-1111-111111111111"
        payload = plan()
        names = {item["type"]: item["name"] for item in payload["naming"]["resources"]}
        payload["pocDesign"].update({
            "resourceGroup": names["resourceGroup"], "operatorObjectId": "fixture-operator",
        })
        operator = {"id": "fixture-operator", "displayName": "Fixture", "userPrincipalName": "fixture@example.invalid"}
        image = names["containerRegistry"] + ".azurecr.io/model-service-poc:live-fixture"
        actual_image = image
        group_tags = {"app-onboard-skill": "true", "app-onboard-session-id": "fixture-session"}
        outputs = {
            "containerRegistryLoginServer": {"value": names["containerRegistry"] + ".azurecr.io"},
            "foundryRunnerFqdn": {"value": "fixture.azurecontainerapps.io"},
            "apimGatewayUrl": {"value": "https://fixture.azure-api.net"},
            "keyVaultName": {"value": names["keyVault"]},
        }

        def metadata(arguments, *args, **kwargs):
            if arguments[:2] == ["account", "show"]:
                return {"tenantId": "fixture-tenant", "state": "Enabled"}
            if arguments[0] == "rest":
                return operator
            if arguments[:2] == ["group", "show"]:
                return {"tags": group_tags}
            if arguments[:3] == ["deployment", "sub", "create"]:
                return outputs
            self.fail(f"Unexpected metadata command: {arguments}")

        def output(arguments, *args, **kwargs):
            if arguments[:2] == ["group", "exists"]:
                return "true"
            if arguments[:2] == ["containerapp", "show"]:
                return actual_image
            if arguments[:2] == ["provider", "show"]:
                return "Registered"
            self.fail(f"Unexpected or paid build command: {arguments}")

        with TemporaryDirectory(prefix="poc-reuse-image-test-") as directory:
            path = Path(directory) / "prepare-plan.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            (path.parent / "context.json").write_text(json.dumps({
                "sessionId": "fixture-session",
                "azure": {
                    "subscriptionId": subscription, "tenantId": "fixture-tenant",
                    "resourceGroup": names["resourceGroup"], "region": "eastus2",
                },
            }), encoding="utf-8")
            (path.parent / "live-deployment.json").write_text(json.dumps({
                "status": "configuration_healthy", "subscriptionId": subscription,
                "resourceGroup": names["resourceGroup"], "region": "eastus2", "image": image,
            }), encoding="utf-8")
            with patch("scripts.deploy_live_pocs.az_json", side_effect=metadata), patch(
                "scripts.deploy_live_pocs.az_output", side_effect=output,
            ), patch("scripts.deploy_live_pocs.secret_values", return_value={
                name: "SYNTHETIC_PARAMETER_VALUE_" + name for name in SECRET_PARAMETERS
            }), patch(
                "scripts.deploy_live_pocs.completed_deployment", return_value=(outputs, "original-time"),
            ) as completed, patch("scripts.deploy_live_pocs.preview") as preview_call, patch(
                "scripts.deploy_live_pocs.healthy", return_value={"instance_id": "fixture-instance"},
            ):
                result = deploy(path, subscription, resume=True, reuse_image=True)
                self.assertEqual(result["image"], image)
                self.assertFalse(completed.call_args.kwargs["match_template"])
                self.assertEqual(preview_call.call_count, 1)
                actual_image = "unexpected-image"
                with self.assertRaisesRegex(RuntimeError, "deployed image changed"):
                    deploy(path, subscription, resume=True, reuse_image=True)
                actual_image = image
                group_tags = {
                    "app-onboard-skill": "true", "app-onboard-session-id": "another-session",
                    "unrelated-tag": "fixture-session",
                }
                with self.assertRaisesRegex(RuntimeError, "not tagged"):
                    deploy(path, subscription, resume=True, reuse_image=True)

    def test_local_bicep_has_no_subscription_flag_but_cloud_commands_remain_scoped(self):
        subscription = "11111111-1111-1111-1111-111111111111"
        with patch("scripts.cloud_cli.shutil.which", return_value="az"), patch(
            "scripts.cloud_cli.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "{}", ""),
        ) as process:
            az_json(["bicep", "build", "--file", "main.bicep", "--stdout"], subscription)
            self.assertNotIn("--subscription", process.call_args.args[0])
            az_json(["group", "show", "--name", "fixture"], subscription)
            self.assertIn("--subscription", process.call_args.args[0])
            self.assertIn(subscription, process.call_args.args[0])

    def test_gateway_bootstrap_quotas_and_policy_literals_match_the_deployment_contract(self):
        source = (ROOT / "infra" / "live" / "modules" / "api-management.bicep").read_text(encoding="utf-8")
        for name in ("modelPolicy", "foundryPolicy", "pilotPolicy"):
            section = source.split(f"resource {name} ", 1)[1].split("\nresource ", 1)[0]
            body = re.search(r"'''\s*(<policies>.*?</policies>)\s*'''", section, re.S)
            self.assertIsNotNone(body)
            assert body is not None
            policy = ET.fromstring(body.group(1))
            self.assertNotRegex(body.group(1), r"'__(?:TENANT|CUSTOMER)_")
            quota = policy.findall("./inbound/quota")
            if name == "pilotPolicy":
                self.assertEqual(len(quota), 1)
                self.assertEqual(quota[0].attrib, {"calls": "7920", "renewal-period": "3600"})
                self.assertEqual(
                    [item.attrib for item in quota[0].findall("api")],
                    [{"id": "model", "calls": "720"}, {"id": "foundry", "calls": "7200"}],
                )
            else:
                self.assertEqual(quota, [])
            if name == "modelPolicy":
                header = policy.find("./outbound/set-header[@name='x-poc-tenant-id']")
                self.assertIsNotNone(header)
                assert header is not None
                self.assertEqual(header.attrib["exists-action"], "override")
                self.assertEqual(header.findtext("value"), '@((string)context.Variables["pocTenant"])')
        declarations = re.findall(r"^resource (\w+) '[^']+' = ([^{]*)\{", source, re.M)
        for name, condition in declarations:
            if name != "apiManagement":
                self.assertEqual(condition.strip(), "if (!isPlaceholder)", name)
        self.assertIn("verbosity: 'error'", source)
        self.assertNotIn("verbosity: 'errors'", source)

    def test_operator_lookup_uses_subscription_qualified_rest_and_stops_changed_identity(self):
        subscription = "11111111-1111-1111-1111-111111111111"
        payload = plan()
        payload["pocDesign"].update({
            "resourceGroup": "fixture-group", "operatorObjectId": "fixture-operator",
        })
        with TemporaryDirectory(prefix="poc-operator-test-") as directory:
            path = Path(directory) / "prepare-plan.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            (path.parent / "context.json").write_text(json.dumps({
                "sessionId": "fixture-session",
                "azure": {
                    "subscriptionId": subscription, "tenantId": "fixture-tenant",
                    "resourceGroup": "fixture-group",
                },
            }), encoding="utf-8")
            with patch("scripts.deploy_live_pocs.az_json", side_effect=[
                {"tenantId": "fixture-tenant", "state": "Enabled"},
                {"id": "changed-operator"},
            ]) as command, patch("scripts.deploy_live_pocs.az_output") as outside:
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    deploy(path, subscription, resume=False)
                arguments = command.call_args_list[1].args[0]
                self.assertEqual(arguments[:3], ["rest", "--method", "GET"])
                self.assertIn("Content-Type=application/json", arguments)
                self.assertEqual(command.call_args_list[1].args[1], subscription)
                outside.assert_not_called()

    def test_cli_audit_records_outcomes_and_redacts_credentials(self):
        credential = "SYNTHETIC_CREDENTIAL_DO_NOT_LOG"
        with TemporaryDirectory(prefix="poc-audit-test-") as directory:
            path = Path(directory) / "audit.log"
            with audit_commands(path), patch("scripts.cloud_cli.shutil.which", return_value="az"), patch(
                "scripts.cloud_cli.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, '{"ok":true}', ""),
                    subprocess.CompletedProcess([], 1, "", "failed " + credential),
                    subprocess.TimeoutExpired("az", 1),
                ],
            ):
                subscription = "11111111-1111-1111-1111-111111111111"
                self.assertEqual(az_json(["account", "show"], subscription), {"ok": True})
                with self.assertRaisesRegex(CloudCommandError, r"failed \[redacted\]"):
                    az_output(["fixture", credential], subscription, secret_values=(credential,))
                with self.assertRaisesRegex(CloudCommandError, "timed out"):
                    az_output(["fixture"], subscription)
            content = path.read_text(encoding="utf-8")
            self.assertNotIn(credential, content)
            self.assertIn("[redacted]", content)
            self.assertEqual(
                [line.rsplit(" | ", 1)[1] for line in content.splitlines()],
                ["started", "succeeded", "started", "failed", "started", "failed"],
            )

    def test_preview_stops_deletions_and_persists_only_change_metadata(self):
        with TemporaryDirectory(prefix="poc-preview-test-") as directory:
            path = Path(directory) / "preview.json"
            subscription = "11111111-1111-1111-1111-111111111111"
            with patch("scripts.deploy_live_pocs.az_json", return_value={
                "changes": [{"resourceId": "/fixture", "changeType": "Delete"}],
            }), self.assertRaisesRegex(RuntimeError, "contains a deletion"):
                preview([], subscription, name="fixture", redactions=(), output_path=path)
            self.assertFalse(path.exists())
            with patch("scripts.deploy_live_pocs.az_json", return_value={
                "changes": [{
                    "resourceId": "/fixture", "changeType": "Create",
                    "after": {"value": "SYNTHETIC_SECRET_MUST_NOT_BE_PERSISTED"},
                }],
            }):
                preview([], subscription, name="fixture", redactions=(), output_path=path)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                [{"resourceId": "/fixture", "changeType": "Create"}],
            )

    def test_parameters_are_complete_and_do_not_include_paused_fusion_resources_or_credentials(self):
        values = {name: "SYNTHETIC_PARAMETER_VALUE_" + name for name in SECRET_PARAMETERS}
        payload = parameters(
            plan(),
            {"id": "fixture-operator", "displayName": "Fixture", "userPrincipalName": "fixture@example.invalid"},
            values, placeholder=False, image="fixture.azurecr.io/poc:immutable-tag",
        )
        data = payload["parameters"]
        self.assertEqual(data["isPlaceholder"]["value"], False)
        self.assertEqual(data["modelCapacity"]["value"], 3)
        self.assertEqual(data["resourceGroupName"]["value"], "fixture-resourceGroup")
        self.assertTrue(SECRET_PARAMETERS <= data.keys())
        self.assertFalse(any("github" in key.lower() or "fusion" in key.lower() for key in data))
        json.dumps(payload)

    def test_unexpected_credential_fields_cannot_enter_the_deployment(self):
        with self.assertRaisesRegex(RuntimeError, "Unexpected deployment credential"):
            parameters(
                plan(),
                {"id": "fixture-operator", "displayName": "Fixture", "userPrincipalName": "fixture@example.invalid"},
                {"githubToken": "SHOULD_NEVER_BE_UPLOADED"},
                placeholder=True, image="fixture",
            )

    def test_plaintext_parameters_cannot_be_written_into_the_build_context(self):
        with self.assertRaisesRegex(RuntimeError, "outside the repository"):
            write_private_parameters(ROOT, {"parameters": {"synthetic": {"value": "value"}}})
        with TemporaryDirectory(prefix="poc-params-test-") as directory:
            path = write_private_parameters(Path(directory), {"parameters": {}})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"parameters": {}})

    @unittest.skipUnless(os.name == "nt", "The administration checkpoint intentionally uses Windows DPAPI")
    def test_private_checkpoint_is_encrypted_and_bound_to_its_purpose(self):
        with TemporaryDirectory(prefix="poc-dpapi-test-") as directory:
            path = Path(directory) / "checkpoint.bin"
            original = b'{"value":"SYNTHETIC_SECRET_MUST_NOT_BE_PLAINTEXT"}'
            save(path, original, "fixture-session")
            self.assertNotIn(b"SYNTHETIC_SECRET", path.read_bytes())
            self.assertEqual(load(path, "fixture-session"), original)
            with self.assertRaises(RuntimeError):
                load(path, "wrong-session")
