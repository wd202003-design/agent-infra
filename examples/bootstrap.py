import argparse
import json
import os
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--backend", choices=["demo", "opensandbox"], default="demo")
    parser.add_argument("--pool", default="demo")
    parser.add_argument("--location", choices=["server", "local", "edge"], default="server")
    parser.add_argument("--name", default="first-runner")
    parser.add_argument("--output", default=".local/node.env")
    args = parser.parse_args()
    url = httpx.URL(args.url)
    if url.scheme != "https" and url.host not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Remote registration requires HTTPS")
    if url.query or url.fragment or url.userinfo:
        raise SystemExit("Use a base URL without embedded credentials, query or fragment")
    root = Path(__file__).resolve().parents[1]
    output = Path(args.output)
    if output.exists() and "AGENT_NODE_TOKEN=" in output.read_text():
        raise SystemExit("Existing node credential found; use another output or revoke the old node first")
    token = (root / ".local/admin.token").read_text().strip()
    with httpx.Client(base_url=args.url, headers={"Authorization": "Bearer " + token}, timeout=15, follow_redirects=False) as client:
        for agent in json.loads((root / "examples/agents.json").read_text()):
            response = client.post("/v1/agents", json=agent)
            response.raise_for_status()
        response = client.post("/v1/nodes", json={"name": args.name, "pool": args.pool,
                                                 "backend": args.backend, "location": args.location})
        response.raise_for_status()
        node = response.json()
    os.umask(0o077)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("AGENT_NODE_TOKEN=" + node["token"] + "\nAGENT_CONTROL_URL=" + args.url + "\n")
    output.chmod(0o600)
    print("Registered node", node["node"]["id"], "credential saved to", output)


if __name__ == "__main__":
    main()
