"""Run from repository root with PYTHONPATH=control-plane:."""
import json
from pathlib import Path
import tempfile

from infra_control.app import create_app
from infra_control.db import Database


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database("sqlite:" + tmp + "/schema.db", test_sqlite=True)
        app = create_app(db, [{"id":"schema-export", "role":"admin", "pools":[], "token_sha256":"0"*64}])
        target = Path(__file__).with_name("openapi-v1.json")
        target.write_text(json.dumps(app.openapi(), ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        print(target)


if __name__ == "__main__":
    main()
