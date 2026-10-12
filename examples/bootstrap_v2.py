"""Register an offline demo Agent and policy with a short-lived admin token."""

import os
import uuid
import httpx


def main():
    with httpx.Client(
        base_url=os.environ["AGENT_CONTROL_URL"].rstrip("/"),
        headers={"Authorization": "Bearer " + os.environ["AGENT_ADMIN_TOKEN"]},
        timeout=20,
        follow_redirects=False,
    ) as client:

        def post(path, body):
            response = client.post(
                "/v2" + path, json=body, headers={"Idempotency-Key": str(uuid.uuid4())}
            )
            response.raise_for_status()
            return response.json()["data"]

        post(
            "/policies/offline/versions",
            {
                "version": "1",
                "allowed_pools": ["demo"],
                "tool_kinds": ["python", "shell", "browser"],
            },
        )
        post(
            "/agents/calculate/versions",
            {
                "agent_id": "calculate",
                "version": "1",
                "description": "隔离计算示例",
                "source": "print(6 * 7)",
                "policy_ref": {"id": "offline", "version": "1"},
            },
        )
        print(
            "Policy offline:1 and Agent calculate:1 are ready. Open /v2.html to submit."
        )


if __name__ == "__main__":
    main()
