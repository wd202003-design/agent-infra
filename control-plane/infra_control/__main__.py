import argparse
import os

from .db import Database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["migrate", "serve"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if args.command == "migrate":
        Database(os.environ["AGENT_DATABASE_URL"]).migrate()
        print("database migrations complete")
    else:
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
