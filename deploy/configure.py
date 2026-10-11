"""Generate private local credentials without emitting tokens to logs."""
import hashlib
import json
import os
from pathlib import Path
import secrets


def main():
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1] / ".local"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any((root / p).exists() for p in ["platform.env", "admin.token", "user.token"]):
        raise SystemExit("Existing credentials found; refusing to overwrite")
    users = []
    for role in ["admin", "user"]:
        token = secrets.token_urlsafe(32)
        (root / (role + ".token")).write_text(token)
        users.append({"id": role, "role": role, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                      "pools": ["demo", "server", "local", "edge"]})
    password = secrets.token_urlsafe(32)
    (root / "platform.env").write_text("POSTGRES_PASSWORD=" + password + "\nAGENT_USERS_JSON='" + json.dumps(users) + "'\n")
    if not (root / "node.env").exists():
        (root / "node.env").write_text("# Run examples/bootstrap.py before starting Runner\n")
    print("Created .local/platform.env, admin.token and user.token (private permissions).")


if __name__ == "__main__":
    main()
