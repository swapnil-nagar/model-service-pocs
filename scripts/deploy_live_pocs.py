import argparse
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from model_service.worker import NoRedirect
from scripts.cloud_cli import CloudCommandError, audit_commands, az_json, az_output, subscription_id
from scripts.private_checkpoint import load, save

ROOT = Path(__file__).resolve().parents[1]
SECRET_PARAMETERS = {
    "foundryRunnerBackendKey",
    "tenantAPrimaryKey", "tenantASecondaryKey",
    "tenantBPrimaryKey", "tenantBSecondaryKey",
}
PROVIDERS = (
    "Microsoft.ApiManagement", "Microsoft.CognitiveServices", "Microsoft.App",
    "Microsoft.ContainerRegistry", "Microsoft.KeyVault",
    "Microsoft.OperationalInsights", "Microsoft.Insights", "Microsoft.ManagedIdentity",
)


def required_string(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Missing or invalid deployment field: {key}")
    return value


def object_data(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid deployment object: {label}")
    return value


def secret_values(session: Path, subscription: str, resource_group: str, session_id: str) -> dict:
    path = session / "deployment-secrets.dpapi"
    if path.exists():
        checkpoint = object_data(json.loads(load(path, session_id)), "private checkpoint")
        if (
            checkpoint.get("subscription") != subscription
            or checkpoint.get("resource_group") != resource_group
            or checkpoint.get("session_id") != session_id
        ):
            raise RuntimeError("The private checkpoint belongs to a different deployment scope")
        values = object_data(checkpoint.get("secrets"), "checkpoint secrets")
        if set(values) != SECRET_PARAMETERS or not all(
            isinstance(value, str) and len(value) >= 24 and value.isascii()
            for value in values.values()
        ):
            raise RuntimeError("The private checkpoint has invalid credential fields")
        return values
    values = {name: secrets.token_hex(24) for name in SECRET_PARAMETERS}
    save(
        path,
        json.dumps({
            "subscription": subscription, "resource_group": resource_group,
            "session_id": session_id, "secrets": values,
        }).encode("utf-8"),
        session_id,
    )
    return values


def parameters(
    plan: dict, operator: dict, values: dict, *,
    placeholder: bool, image: str, created_at: str | None = None,
) -> dict:
    naming = object_data(plan.get("naming"), "naming")
    variables = object_data(plan.get("deploymentVariables"), "deployment variables")
    design = object_data(plan.get("pocDesign"), "POC design")
    resources = {
        required_string(item, "type"): required_string(item, "name")
        for item in naming.get("resources", [])
        if isinstance(item, dict)
    }
    mapping = {
        "resourceGroupName": "resourceGroup",
        "foundryAccountName": "foundryAccount",
        "modelDeploymentName": "modelDeployment",
        "apimName": "apiManagement",
        "containerEnvironmentName": "containerEnvironment",
        "foundryRunnerName": "foundryRunner",
        "acrName": "containerRegistry",
        "keyVaultName": "keyVault",
        "logAnalyticsName": "logAnalytics",
        "appInsightsName": "applicationInsights",
        "foundryIdentityName": "foundryIdentity",
    }
    if "fusionRunner" in resources or "fusionIdentity" in resources:
        raise RuntimeError("Fusion resources are not authorized in this deployment scope")
    data: dict[str, object] = {key: resources[kind] for key, kind in mapping.items()}
    data.update(
        environmentName=required_string(naming, "resourcePrefix"),
        location=required_string(variables, "location"),
        sessionId=required_string(variables, "sessionId"),
        deployedBy=required_string(operator, "displayName"),
        createdAt=created_at if created_at is not None else datetime.now(timezone.utc).isoformat(),
        deployerObjectId=required_string(operator, "id"),
        publisherEmail=required_string(operator, "userPrincipalName"),
        publisherName="Model Service POC",
        tenantASubscriptionId=required_string(design, "tenantASubscriptionId"),
        tenantBSubscriptionId=required_string(design, "tenantBSubscriptionId"),
        modelName=required_string(design, "modelName"),
        modelVersion=required_string(design, "modelVersion"),
        modelCapacity=design["modelCapacity"],
        containerImage=image,
        isPlaceholder=placeholder,
    )
    if set(values) != SECRET_PARAMETERS:
        raise RuntimeError("Unexpected deployment credential fields")
    data.update(values)
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {key: {"value": value} for key, value in data.items()},
    }


def write_private_parameters(directory: Path, payload: dict) -> Path:
    if directory.resolve().is_relative_to(ROOT):
        raise RuntimeError("Plaintext parameters must be outside the repository/image build context")
    path = directory / "parameters.json"
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream)
    return path


def healthy(fqdn: str) -> dict:
    opener = build_opener(ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            with opener.open("https://" + fqdn + "/healthz", timeout=20) as response:
                body = json.load(response)
            if (
                isinstance(body, dict)
                and body.get("status") == "configured"
                and body.get("mode") == "foundry"
                and body.get("live_enabled") is True
            ):
                return body
        except (URLError, OSError, ValueError):
            pass
        print("Waiting for the real Foundry runner configuration health check...")
        time.sleep(5)
    raise RuntimeError("The real runner did not pass configuration health; placeholder health is not accepted")


def preview(
    common: list[str], subscription: str, *, name: str,
    redactions: tuple[str, ...], output_path: Path,
) -> None:
    prediction = object_data(
        az_json(
            ["deployment", "sub", "what-if", "--name", name,
             *common, "--no-pretty-print", "--result-format", "ResourceIdOnly"],
            subscription, timeout=600, secret_values=redactions,
        ),
        "ARM what-if",
    )
    changes = prediction.get("changes")
    if not isinstance(changes, list):
        properties = prediction.get("properties")
        changes = properties.get("changes") if isinstance(properties, dict) else None
    if not isinstance(changes, list):
        raise RuntimeError("ARM what-if did not return verifiable change metadata")
    if any(isinstance(change, dict) and change.get("changeType") == "Delete" for change in changes):
        raise RuntimeError("ARM what-if contains a deletion; stopping for review")
    output_path.write_text(
        json.dumps([
            {"resourceId": change.get("resourceId"), "changeType": change.get("changeType")}
            for change in changes if isinstance(change, dict)
        ], indent=2) + "\n", encoding="utf-8",
    )


def completed_deployment(
    name: str, template: Path, expected: dict, subscription: str,
    redactions: tuple[str, ...], *, match_template: bool = True,
) -> tuple[dict, str] | None:
    deployed = object_data(
        az_json(
            ["deployment", "sub", "show", "--name", name, "--query", "properties"],
            subscription, secret_values=redactions,
        ),
        "previous deployment",
    )
    state = deployed.get("provisioningState")
    if state in {"Failed", "Canceled"}:
        print("Previous deployment did not succeed; reapplying the approved desired state.")
        return None
    if state != "Succeeded":
        raise RuntimeError("The previous deployment is not terminal; do not overlap its deployment")
    if match_template:
        compiled = object_data(
            az_json(["bicep", "build", "--file", str(template), "--stdout"], subscription),
            "compiled template",
        )
        generator = object_data(
            object_data(compiled.get("metadata"), "template metadata").get("_generator"),
            "template generator",
        )
        if deployed.get("templateHash") != required_string(generator, "templateHash"):
            print("Infrastructure source changed; reapplying the approved foundation.")
            return None
    actual = object_data(deployed.get("parameters"), "deployment parameters")
    for key, entry in object_data(expected.get("parameters"), "expected parameters").items():
        if key in SECRET_PARAMETERS or key == "createdAt":
            continue
        if object_data(actual.get(key), key).get("value") != object_data(entry, key).get("value"):
            print(f"Deployment parameter changed: {key}; reapplying the approved desired state.")
            return None
    created_at = required_string(object_data(actual.get("createdAt"), "createdAt"), "value")
    return object_data(deployed.get("outputs"), "foundation outputs"), created_at


def deploy(plan_path: Path, subscription: str, *, resume: bool, reuse_image: bool = False) -> dict:
    if reuse_image and not resume:
        raise RuntimeError("--reuse-image requires --resume on a verified deployment")
    session = plan_path.resolve().parent
    plan = object_data(json.loads(plan_path.read_text(encoding="utf-8")), "plan")
    context = object_data(json.loads((session / "context.json").read_text(encoding="utf-8")), "context")
    target = object_data(context.get("azure"), "Azure target")
    design = object_data(plan.get("pocDesign"), "POC design")
    if required_string(target, "subscriptionId") != subscription:
        raise RuntimeError("Explicit subscription does not match the approved plan")
    resource_group = required_string(design, "resourceGroup")
    if resource_group != required_string(target, "resourceGroup"):
        raise RuntimeError("Resource group does not match the approved scope")
    session_id = required_string(context, "sessionId")
    names = {
        item["type"]: item["name"]
        for item in object_data(plan.get("naming"), "naming")["resources"]
    }
    account = object_data(az_json(["account", "show"], subscription), "Azure account")
    if account.get("tenantId") != target.get("tenantId") or account.get("state") != "Enabled":
        raise RuntimeError("The Azure tenant/account does not match the approved enabled target")
    operator = object_data(
        az_json(
            ["rest", "--method", "GET",
             "--url", "https://graph.microsoft.com/v1.0/me?$select=id,displayName,userPrincipalName",
             "--headers", "Content-Type=application/json"],
            subscription,
        ),
        "Azure operator",
    )
    if operator.get("id") != design.get("operatorObjectId"):
        raise RuntimeError("Azure operator identity changed; review authorization before deployment")
    exists = az_output(["group", "exists", "--name", resource_group, "-o", "tsv"], subscription)
    if exists.lower() == "true":
        if not resume:
            raise RuntimeError("Target resource group exists; use --resume only for this owned pilot")
        group = object_data(
            az_json(["group", "show", "--name", resource_group], subscription), "existing group"
        )
        tags = object_data(group.get("tags"), "existing group tags")
        if tags.get("app-onboard-session-id") != session_id or tags.get("app-onboard-skill") != "true":
            raise RuntimeError("Refusing to modify an existing group not tagged as this deployment session")
    elif exists.lower() != "false":
        raise RuntimeError("Could not verify the resource-group ownership boundary")
    image_to_reuse = None
    if (session / "live-deployment.json").exists():
        previous = object_data(
            json.loads((session / "live-deployment.json").read_text(encoding="utf-8")),
            "previous deployment",
        )
        if previous.get("status") == "configuration_healthy":
            if not reuse_image:
                raise RuntimeError("This deployment already completed; review an image-update plan rather than re-running bootstrap")
            if (
                previous.get("subscriptionId") != subscription
                or previous.get("resourceGroup") != resource_group
                or required_string(previous, "region") != required_string(target, "region")
            ):
                raise RuntimeError("The verified image record belongs to a different deployment scope")
            image_to_reuse = required_string(previous, "image")
            if not image_to_reuse.startswith(names["containerRegistry"] + ".azurecr.io/model-service-poc:"):
                raise RuntimeError("The verified image is not in this pilot's registry")
            current_image = az_output(
                ["containerapp", "show", "--name", names["foundryRunner"], "--resource-group", resource_group,
                 "--query", "properties.template.containers[0].image", "-o", "tsv"],
                subscription,
            )
            if current_image != image_to_reuse:
                raise RuntimeError("The deployed image changed; review drift before reusing it")
    if reuse_image and image_to_reuse is None:
        raise RuntimeError("--reuse-image requires a verified real-image deployment record")
    for namespace in PROVIDERS:
        state = az_output(
            ["provider", "show", "--namespace", namespace, "--query", "registrationState", "-o", "tsv"],
            subscription,
        )
        if state != "Registered":
            print(f"Registering planned provider: {namespace}")
            az_output(["provider", "register", "--namespace", namespace, "--wait"], subscription, timeout=600)
    values = secret_values(session, subscription, resource_group, session_id)
    redactions = tuple(values.values())
    location = required_string(object_data(plan.get("deploymentVariables"), "variables"), "location")
    template = ROOT / "infra" / "live" / "main.bicep"
    suffix = required_string(object_data(plan.get("naming"), "naming"), "suffix")
    bootstrap = parameters(
        plan, operator, values, placeholder=image_to_reuse is None,
        image=image_to_reuse if image_to_reuse is not None else "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest",
    )
    if image_to_reuse is not None:
        completed = completed_deployment(
            "poc-workload-" + suffix, template, bootstrap, subscription, redactions, match_template=False,
        )
        if completed is None:
            raise RuntimeError("The previous workload must have succeeded with matching parameters before image reuse")
    else:
        completed = completed_deployment(
            "poc-foundation-" + suffix, template, bootstrap, subscription, redactions,
        ) if resume else None
    with TemporaryDirectory(prefix="modelpoc-parameters-") as directory:
        private_file = write_private_parameters(Path(directory), bootstrap)
        common = [
            "--location", location, "--template-file", str(template),
            "--parameters", "@" + str(private_file),
        ]
        if completed is None:
            preview(
                common, subscription, name="poc-preview-foundation-" + suffix,
                redactions=redactions, output_path=session / "what-if-summary.json",
            )
            print("Provisioning the approved fixed-template foundation...")
            outputs = object_data(
                az_json(
                    ["deployment", "sub", "create", "--name", "poc-foundation-" + suffix,
                     *common, "--query", "properties.outputs"],
                    subscription, timeout=1200, secret_values=redactions,
                ),
                "foundation outputs",
            )
            created_at = required_string(bootstrap["parameters"]["createdAt"], "value")
            print("Waiting 60 seconds for managed-identity role propagation...")
            time.sleep(60)
        else:
            outputs, created_at = completed
            print("Reusing the succeeded deployment after required scope and parameter verification.")

        def output(name: str) -> str:
            return required_string(object_data(outputs.get(name), name), "value")

        registry_server = output("containerRegistryLoginServer")
        registry = names["containerRegistry"]
        if image_to_reuse is not None:
            image = image_to_reuse
            print("Reusing the verified running image; no registry build will run.")
        else:
            tag = "live-" + uuid.uuid4().hex[:12]
            image = registry_server + "/model-service-poc:" + tag
            print("Building the real Foundry runner image through ACR...")
            az_output(
                ["acr", "build", "--registry", registry, "--image", "model-service-poc:" + tag,
                 "--file", str(ROOT / "Dockerfile.live"), "--platform", "linux", "--no-logs",
                 "--timeout", "600", str(ROOT)],
                subscription, timeout=1200, secret_values=redactions,
            )
        write_private_parameters(
            Path(directory),
            parameters(plan, operator, values, placeholder=False, image=image, created_at=created_at),
        )
        preview(
            common, subscription, name="poc-preview-workload-" + suffix,
            redactions=redactions, output_path=session / "what-if-workload.json",
        )
        print("Applying the real-image workload and managed gateway configuration...")
        outputs = object_data(
            az_json(
                ["deployment", "sub", "create", "--name", "poc-workload-" + suffix,
                 *common, "--query", "properties.outputs"],
                subscription, timeout=1200, secret_values=redactions,
            ),
            "workload outputs",
        )
        configured = healthy(output("foundryRunnerFqdn"))
        actual_image = az_output(
            ["containerapp", "show", "--name", names["foundryRunner"], "--resource-group", resource_group,
             "--query", "properties.template.containers[0].image", "-o", "tsv"],
            subscription,
        )
        if actual_image != image:
            raise RuntimeError("The active application image does not match the real built image")
        result = {
            "status": "configuration_healthy",
            "subscriptionId": subscription,
            "resourceGroup": resource_group,
            "region": location,
            "gatewayUrl": output("apimGatewayUrl"),
            "keyVaultName": output("keyVaultName"),
            "runnerFqdn": output("foundryRunnerFqdn"),
            "image": image,
            "instanceId": configured["instance_id"],
            "inferenceVerified": False,
            "fusion": "not_deployed_feature_enablement_blocked",
        }
        (session / "live-deployment.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision only the reviewed Foundry/APIM fixed-template POC.")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--subscription", type=subscription_id, required=True)
    parser.add_argument("--approve-resource-provisioning", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reuse-image", action="store_true")
    args = parser.parse_args()
    if not args.approve_resource_provisioning:
        parser.error("--approve-resource-provisioning is required after reviewing scope and costs")
    if args.reuse_image and not args.resume:
        parser.error("--reuse-image requires --resume")
    try:
        with audit_commands(args.plan.resolve().parent / "deploy-audit.log"):
            result = deploy(args.plan, args.subscription, resume=args.resume, reuse_image=args.reuse_image)
    except (CloudCommandError, RuntimeError) as exc:
        print(f"Deployment stopped: {exc}")
        raise SystemExit(1) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
