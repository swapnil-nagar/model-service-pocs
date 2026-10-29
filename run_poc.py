import argparse
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from model_service.contracts import Approach, Request
from model_service.fusion import run_fusion
from model_service.mocks import LocalExperiment


def run(approach: Approach) -> dict:
    with LocalExperiment(approach) as experiment:
        def invoke(tenant: str) -> tuple[str, dict, float]:
            start = time.perf_counter()
            request = Request(tenant, "assistant", "same-session", "chat", f"Hello {tenant}")
            result = experiment.service.run(experiment.customers[tenant].caller_token, request)
            content = json.loads(result.content)
            if content["tenant"] != tenant or content["user_prompts"] != [request.prompt]:
                raise RuntimeError("Tenant isolation assertion failed")
            return tenant, {
                "worker_pid": result.worker_pid,
                "workspace_id": result.workspace_id,
                "turn": result.turn,
            }, (time.perf_counter() - start) * 1000

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(invoke, experiment.customers))
        workspaces = [result[1]["workspace_id"] for result in results]
        pids = [result[1]["worker_pid"] for result in results]
        if len(set(workspaces)) != len(workspaces):
            raise RuntimeError("Per-execution workspace isolation assertion failed")
        if len(set(pids)) != len(pids):
            raise RuntimeError("Per-execution process isolation assertion failed")
        return {
            "approach": approach.value,
            "mode": "local_mock_only",
            "tenant_calls": len(results),
            "distinct_workspaces": len(set(workspaces)),
            "requests": [
                {"tenant": tenant, **metadata, "elapsed_ms": round(elapsed, 2)}
                for tenant, metadata, elapsed in results
            ],
            "mock_backend_calls": sum(
                len(endpoint.calls) for endpoint in experiment.backends.values()
            ),
            "azure_network_compute_isolation": "not_tested",
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run local POCs; no external model calls.")
    parser.add_argument("--approach", choices=["all", *Approach, "github-fusion"], default="all")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.approach == "all":
        results = [run(approach) for approach in Approach]
        results.append(asyncio.run(run_fusion()))
    elif args.approach == "github-fusion":
        results = [asyncio.run(run_fusion())]
    else:
        results = [run(Approach(args.approach))]
    report = {
        "schema_version": 2,
        "note": "Local HTTP/process or SDK-mock timings, NOT model inference benchmarks.",
        "results": results,
    }
    encoded = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
