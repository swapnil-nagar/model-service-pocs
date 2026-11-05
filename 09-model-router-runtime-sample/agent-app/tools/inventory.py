import json
import logging
import os

from azure_functions_agents import tool
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("poc09.inventory")


class StockQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(description="Inventory SKU. This fixture supports A-101 only.")


def stock_result(sku: str, customer: str) -> dict[str, str | int]:
    if customer not in {"A", "B"}:
        raise RuntimeError("The operator must configure customer A or B for this runtime.")
    if sku != "A-101":
        raise ValueError("The read-only fixture supports SKU A-101 only.")
    on_hand = 7 if customer == "A" else 2
    reserved = 2
    return {
        "sku": sku,
        "on_hand": on_hand,
        "reserved": reserved,
        "available_to_ship": max(0, on_hand - reserved),
        "source": f"customer-{customer}-inventory",
    }


@tool(schema=StockQuery)
async def lookup_stock(params: StockQuery) -> str:
    """Read authorized inventory and available_to_ship after reserving two units."""
    customer = os.environ.get("POC09_CUSTOMER", "")
    result = stock_result(params.sku, customer)
    logger.info("poc09_tool_executed customer=%s tool=lookup_stock sku=%s", customer, params.sku)
    return json.dumps(result)
