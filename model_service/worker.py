"""One fixed inference operation; never executes customer-authored code."""

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Redirect refused", headers, fp)


def execute(payload: dict) -> dict:
    endpoint = payload["endpoint"]
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return {"error": "local_endpoint_required"}
    approach = payload["approach"]
    headers = {"Content-Type": "application/json"}
    messages = payload["messages"]
    deployment = payload["deployment"]
    if approach == "azure-openai":
        path = f"/openai/deployments/{quote(deployment, safe='')}/chat/completions"
        path += "?api-version=2024-10-21"
        headers["api-key"] = payload["credential"]
        body = {"messages": messages, "max_tokens": 128}
    elif approach == "foundry":
        path = "/openai/v1/chat/completions"
        headers["Authorization"] = f"Bearer {payload['credential']}"
        body = {"model": deployment, "messages": messages, "max_tokens": 128}
    elif approach == "apim":
        path = "/models/chat/completions"
        headers["Authorization"] = f"Bearer {payload['credential']}"
        body = {"model": deployment, "messages": messages, "max_tokens": 128}
    elif approach == "azure-ml":
        path = "/score"
        headers["Authorization"] = f"Bearer {payload['credential']}"
        body = {"input_data": {"messages": messages}, "parameters": {"max_new_tokens": 128}}
    else:
        return {"error": "unknown_approach"}
    request = Request(endpoint + path, json.dumps(body).encode(), headers, method="POST")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=2) as response:
            raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            return {"error": "response_too_large"}
        data = json.loads(raw)
        content = (
            data["output"]
            if approach == "azure-ml"
            else data["choices"][0]["message"]["content"]
        )
        if not isinstance(content, str) or not content:
            return {"error": "invalid_response"}
    except HTTPError as exc:
        return {"error": f"upstream_http_{exc.code}"}
    except (URLError, TimeoutError, OSError):
        return {"error": "upstream_unavailable"}
    except (ValueError, KeyError, IndexError, TypeError):
        return {"error": "invalid_response"}
    return {
        "content": content,
        "worker_pid": os.getpid(),
        "workspace_id": os.path.basename(os.getcwd()),
        "environment_names": sorted(os.environ),
    }


if __name__ == "__main__":
    print(json.dumps(execute(json.load(sys.stdin))))
