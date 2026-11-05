import json
import logging
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Literal

from azure_functions_agents import tool
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("poc09.azure_report")
SUBSCRIPTION = "00000000-0000-0000-0000-000000000009"
RESOURCE_PATH = f"/subscriptions/{SUBSCRIPTION}/resources?api-version=2021-04-01"


class AzureRestParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(description=f"Only this synthetic inventory path is supported: {RESOURCE_PATH}")
    method: Literal["GET"] = "GET"


def inventory_result(customer: str, now: datetime | None = None) -> dict[str, object]:
    if customer not in {"A", "B"}:
        raise RuntimeError("The operator must configure customer A or B for this fixture.")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("The reporting clock must be timezone-aware.")
    fixture = [
        (f"demo-{customer}-web-new", "Microsoft.Web/sites", "eastus2",
         now - timedelta(hours=4), now - timedelta(hours=4)),
        (f"demo-{customer}-storage", "Microsoft.Storage/storageAccounts", "eastus2",
         now - timedelta(days=7), now - timedelta(hours=1)),
        (f"demo-{customer}-web-old", "Microsoft.Web/sites", "westus",
         now - timedelta(days=7), now - timedelta(days=2)),
    ]
    resources: list[dict[str, str]] = []
    changes: Counter[str] = Counter({"created": 0, "updated": 0})
    for name, resource_type, location, created, changed in fixture:
        change = "unchanged"
        if created >= now - timedelta(hours=24):
            change = "created"
        elif changed >= now - timedelta(hours=24):
            change = "updated"
        if change != "unchanged":
            changes[change] += 1
        resources.append({
            "name": name, "type": resource_type, "location": location,
            "resourceGroup": f"synthetic-{customer}", "change": change,
            "createdTime": created.isoformat(), "changedTime": changed.isoformat(),
        })
    return {
        "status": "ok", "source": f"synthetic-customer-{customer}-inventory",
        "synthetic": True, "email_sent": False, "as_of": now.isoformat(),
        "lookback_hours": 24, "changes": dict(changes),
        "summary": {
            "total_resources": len(resources),
            "by_type": dict(Counter(resource["type"] for resource in resources)),
            "by_location": dict(Counter(resource["location"] for resource in resources)),
        },
        "value": resources,
    }


def read_inventory(params: AzureRestParams) -> dict[str, object]:
    if params.path != RESOURCE_PATH or os.environ.get("SUBSCRIPTION_ID") != SUBSCRIPTION:
        logger.error("poc09_report_tool_rejected reason=unsupported_scope_or_path")
        return {
            "status": "error", "synthetic": True, "email_sent": False,
            "error": {"code": "unsupported_scope_or_path",
                      "message": "Only the synthetic inventory path is available; no live Azure access."},
        }
    customer = os.environ.get("POC09_CUSTOMER", "")
    if customer not in {"A", "B"}:
        logger.error("poc09_report_tool_rejected reason=invalid_customer")
        return {
            "status": "error", "synthetic": True, "email_sent": False,
            "error": {"code": "invalid_customer", "message": "The operator must configure customer A or B."},
        }
    result = inventory_result(customer)
    logger.info("poc09_tool_executed customer=%s tool=azure_rest source=synthetic email_sent=false", customer)
    return result


@tool(schema=AzureRestParams)
async def azure_rest(params: AzureRestParams) -> str:
    """Read synthetic Azure inventory and authoritative summary/change counts. No Azure access or writes."""
    return json.dumps(read_inventory(params))
