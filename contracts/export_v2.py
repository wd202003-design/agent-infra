"""Generate independent schemas without credentials or external services."""

import json
from pathlib import Path
import tempfile
from infra_control.db import Database
from infra_control.v2.api import create_api
from infra_control.v2.security import Keys
from infra_control.v2.service import Platform


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database("sqlite:" + tmp + "/schema.db", test_sqlite=True)
        api, internal = create_api(Platform(db, Keys(b"s" * 32)), [])
        for name, app in (("v2", api), ("internal-v2", internal)):
            schema = app.openapi()
            schema.setdefault("components", {})["securitySchemes"] = {
                "Bearer": {"type": "http", "scheme": "bearer"}
            }
            schema["servers"] = [{"url": "/v2" if name == "v2" else "/internal/v2"}]
            for path, methods in schema["paths"].items():
                for operation in methods.values():
                    operation["security"] = (
                        [] if path == "/runner/register" else [{"Bearer": []}]
                    )
            target = Path(__file__).with_name("openapi-" + name + ".json")
            target.write_text(
                json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            )
            print(target)


if __name__ == "__main__":
    main()
