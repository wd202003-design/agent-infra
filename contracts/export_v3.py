"""Generate the independent image-agent/1.0 OpenAPI snapshot."""

import json
from pathlib import Path
from infra_control.v3.api import create_api


def main():
    schema = create_api(None, [], []).openapi()
    schema.setdefault("components", {})["securitySchemes"] = {
        "Bearer": {"type": "http", "scheme": "bearer"}
    }
    schema["servers"] = [{"url": "/v3"}]
    for methods in schema["paths"].values():
        for operation in methods.values():
            operation["security"] = [{"Bearer": []}]
    Path(__file__).with_name("openapi-v3.json").write_text(
        json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
