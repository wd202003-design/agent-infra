"""Privileged verifier component. Exact nftables policy in a Docker network namespace.

Never run in the Runner or guest. No shell, no caller-supplied PID or nft source.
"""

import asyncio
import ipaddress
import json
import os


def rules(proxy_ip, proxy_port, management_cidr, management_port):
    proxy_ip = str(ipaddress.IPv4Address(proxy_ip))
    management_cidr = str(ipaddress.IPv4Network(management_cidr, strict=True))
    if not 1 <= proxy_port <= 65535 or not 1 <= management_port <= 65535:
        raise ValueError("invalid_port")
    base = {"family": "inet", "table": "agent_egress"}
    objects = [{"table": {"family": "inet", "name": "agent_egress"}}]
    for name in ("input", "output", "forward"):
        objects.append(
            {
                "chain": base
                | {
                    "name": name,
                    "type": "filter",
                    "hook": name,
                    "prio": -10,
                    "policy": "drop",
                }
            }
        )

    def match(left, right):
        return {"match": {"op": "==", "left": left, "right": right}}

    def add(chain, expressions):
        objects.append(
            {"rule": base | {"chain": chain, "expr": expressions + [{"accept": None}]}}
        )

    payload = lambda protocol, field: {
        "payload": {"protocol": protocol, "field": field}
    }
    # Localhost is confined to this namespace. No DNS, UDP, IPv6 external or direct Internet.
    add("input", [match({"meta": {"key": "iifname"}}, "lo")])
    # Do not allow Docker's 127.0.0.11 DNS forwarder via the loopback exception.
    for protocol, address in (("ip", "127.0.0.1"), ("ip6", "::1")):
        add(
            "output",
            [
                match({"meta": {"key": "oifname"}}, "lo"),
                match(payload(protocol, "daddr"), address),
                match({"meta": {"key": "l4proto"}}, "tcp"),
            ],
        )
    add(
        "output",
        [
            match(payload("ip", "daddr"), proxy_ip),
            match(payload("tcp", "dport"), proxy_port),
        ],
    )
    add(
        "input",
        [
            match(payload("ip", "saddr"), proxy_ip),
            match(payload("tcp", "sport"), proxy_port),
            match({"ct": {"key": "state"}}, "established"),
        ],
    )
    net = ipaddress.IPv4Network(management_cidr)
    prefix = (
        str(net.network_address)
        if net.prefixlen == 32
        else {"prefix": {"addr": str(net.network_address), "len": net.prefixlen}}
    )
    add(
        "input",
        [
            match(payload("ip", "saddr"), prefix),
            match(payload("tcp", "dport"), management_port),
        ],
    )
    add(
        "output",
        [
            match(payload("ip", "daddr"), prefix),
            match(payload("tcp", "sport"), management_port),
            match({"ct": {"key": "direction"}}, "reply"),
            match({"ct": {"key": "state"}}, "established"),
        ],
    )
    return objects


def normalized(document):
    result = []
    for entry in document["nftables"]:
        if "metainfo" in entry:
            continue
        kind, obj = next(iter(entry.items()))
        obj = {k: v for k, v in obj.items() if k not in {"handle", "index", "use"}}
        result.append({kind: obj})
    # nft emits each chain immediately followed by its rules. Group by chain,
    # retaining rule order (order within a chain is security significant).
    return sorted(
        result,
        key=lambda x: (
            0 if "table" in x else 1,
            next(iter(x.values())).get("chain", next(iter(x.values())).get("name", "")),
            0 if "chain" in x else 1,
        ),
    )


class NetworkGuard:
    def __init__(self, proxy_ip, proxy_port, management_cidr, management_port=44772):
        self.expected = rules(proxy_ip, proxy_port, management_cidr, management_port)

    async def command(self, pid, *args, data=None):
        if (
            type(pid) is not int
            or pid <= 1
            or os.stat(f"/proc/{pid}/ns/net").st_ino
            == os.stat("/proc/self/ns/net").st_ino
        ):
            raise ValueError("dedicated_network_namespace_required")
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/nsenter",
            "--target",
            str(pid),
            "--net",
            "--",
            "/usr/sbin/nft",
            *args,
            stdin=asyncio.subprocess.PIPE if data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(data), 3)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode or len(stdout) > 65536:
            raise ValueError("network_guard_unavailable")
        return stdout

    async def verify(self, detail):
        if detail["HostConfig"]["NetworkMode"] in {
            "none",
            "host",
            "default",
            "bridge",
        } or detail["HostConfig"]["NetworkMode"].startswith("container:"):
            raise ValueError("dedicated_bridge_required")
        pid = detail["State"]["Pid"]
        actual = json.loads(
            await self.command(pid, "-j", "list", "table", "inet", "agent_egress")
        )
        if normalized(actual) != normalized({"nftables": self.expected}):
            raise ValueError("network_guard_mismatch")

    async def install(self, detail):
        mode = detail["HostConfig"]["NetworkMode"]
        if mode in {"none", "host", "default", "bridge"} or mode.startswith(
            "container:"
        ):
            raise ValueError("dedicated_bridge_required")
        pid = detail["State"]["Pid"]
        # One atomic create, never flush an unknown existing policy. Repeat verifies only.
        try:
            await self.verify(detail)
            return
        except ValueError:
            pass
        document = {"nftables": [{"add": obj} for obj in self.expected]}
        await self.command(pid, "-j", "-f", "-", data=json.dumps(document).encode())
        await self.verify(detail)
