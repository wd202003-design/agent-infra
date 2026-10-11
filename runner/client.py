import httpx


class Client:
    def __init__(self, url, token, *, transport=None):
        if len(token) < 24:
            raise ValueError("Node credential required")
        parsed = httpx.URL(url)
        if parsed.scheme != "https" and parsed.host not in {"localhost", "127.0.0.1", "::1", "control"}:
            raise ValueError("Remote control plane requires HTTPS")
        self.http = httpx.AsyncClient(base_url=url.rstrip("/"), headers={"Authorization": "Bearer " + token},
                                      timeout=25, follow_redirects=False, transport=transport)

    async def post(self, path, payload):
        response = await self.http.post(path, json=payload)
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self.http.aclose()
