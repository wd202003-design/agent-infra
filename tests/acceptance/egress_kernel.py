"""Linux CI only: exercise actual nft rules in a disposable network namespace.

Requires root, iproute2 and nftables. Never changes the host nft ruleset.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import uuid

from runner.network_guard import NetworkGuard, normalized


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def main():
    suffix = uuid.uuid4().hex[:6]
    ns, host, guest = "egress-" + suffix, "eh" + suffix, "eg" + suffix
    holder = None
    sockets = []
    run("ip", "netns", "add", ns)
    try:
        run("ip", "link", "add", host, "type", "veth", "peer", "name", guest)
        run("ip", "link", "set", guest, "netns", ns)
        for addr in (
            "10.233.0.1/24",
            "10.233.0.3/24",
            "169.254.169.254/32",
            "fd00:233::1/64",
        ):
            run("ip", "addr", "add", addr, "dev", host)
        run("ip", "link", "set", host, "up")
        for addr in ("10.233.0.2/24", "fd00:233::2/64"):
            run("ip", "-n", ns, "addr", "add", addr, "dev", guest)
        run("ip", "-n", ns, "link", "set", guest, "up")
        run("ip", "-n", ns, "link", "set", "lo", "up")
        run("ip", "-n", ns, "route", "add", "default", "via", "10.233.0.1")
        holder = subprocess.Popen(["ip", "netns", "exec", ns, "sleep", "120"])
        # Wait for exec's setns rather than trusting scheduling.
        for _ in range(100):
            if (
                os.stat(f"/proc/{holder.pid}/ns/net").st_ino
                == os.stat("/var/run/netns/" + ns).st_ino
            ):
                break
            threading.Event().wait(0.01)
        detail = {
            "HostConfig": {"NetworkMode": "egress-ci"},
            "State": {"Pid": holder.pid},
        }
        guard = NetworkGuard("10.233.0.3", 3128, "10.233.0.1/32")

        async def install():
            try:
                await guard.install(detail)
            except Exception:
                actual = json.loads(
                    await guard.command(
                        holder.pid, "-j", "list", "table", "inet", "agent_egress"
                    )
                )
                print("Expected:", normalized({"nftables": guard.expected}))
                print("Actual:", normalized(actual))
                raise

        asyncio.run(install())
        asyncio.run(guard.verify(detail))

        def serve(address, port, ipv6=False, udp=False):
            sock = socket.socket(
                socket.AF_INET6 if ipv6 else socket.AF_INET,
                socket.SOCK_DGRAM if udp else socket.SOCK_STREAM,
            )
            sockets.append(sock)
            sock.bind((address, port))
            if not udp:
                sock.listen()

            def echo():
                try:
                    while True:
                        if udp:
                            data, peer = sock.recvfrom(128)
                            sock.sendto(data, peer)
                        else:
                            conn, _ = sock.accept()
                            conn.sendall(b"ok")
                            conn.close()
                except OSError:
                    pass

            threading.Thread(target=echo, daemon=True).start()

        serve("10.233.0.3", 3128)
        serve("10.233.0.3", 443)
        serve("10.233.0.1", 443)
        serve("169.254.169.254", 80)
        serve("fd00:233::1", 443, ipv6=True)
        serve("10.233.0.3", 53, udp=True)
        program = """
import socket
cases = [("10.233.0.3",3128,False,False,True),("10.233.0.3",443,False,False,False),("10.233.0.1",443,False,False,False),("169.254.169.254",80,False,False,False),("fd00:233::1",443,True,False,False),("10.233.0.3",53,False,True,False)]
for host,port,v6,udp,allowed in cases:
 s=socket.socket(socket.AF_INET6 if v6 else socket.AF_INET,socket.SOCK_DGRAM if udp else socket.SOCK_STREAM);s.settimeout(.4)
 try:
  s.connect((host,port))
  if udp:s.send(b"test")
  actual=bool(s.recv(16))
 except OSError:actual=False
 finally:s.close()
 assert actual==allowed,(host,port,actual,allowed)
print("6 real namespace network checks passed")
"""
        print(run("ip", "netns", "exec", ns, sys.executable, "-c", program))
        # A removed rule must invalidate independent certification.
        run(
            "ip",
            "netns",
            "exec",
            ns,
            "nft",
            "flush",
            "chain",
            "inet",
            "agent_egress",
            "output",
        )
        try:
            asyncio.run(guard.verify(detail))
        except ValueError:
            print("tampered rules rejected")
        else:
            raise AssertionError("tampered policy certified")
    finally:
        for sock in sockets:
            sock.close()
        if holder:
            holder.terminate()
            holder.wait()
        subprocess.run(["ip", "link", "del", host], capture_output=True)
        run("ip", "netns", "del", ns)


if __name__ == "__main__":
    main()
