import argparse
import asyncio
import os

from .client import Client
from .journal import Journal
from .worker import Runner


async def serve(args):
    journal = Journal(args.data)
    client = Client(os.environ["AGENT_CONTROL_URL"], os.environ["AGENT_NODE_TOKEN"])
    runner = Runner(client, journal, args.backend)
    try:
        while True:
            try:
                await runner.tick()
            except Exception as exc:
                # Never include credential-bearing HTTP requests or SDK response bodies.
                print("Runner paused/retrying transport:", type(exc).__name__, flush=True)
            await asyncio.sleep(2)
    finally:
        journal.close()
        await client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["demo", "opensandbox"], default="demo")
    parser.add_argument("--data", default=".local/runner")
    args = parser.parse_args()
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
