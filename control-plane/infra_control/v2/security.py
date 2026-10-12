"""Purpose-bound short credentials. No plaintext tokens in database rows."""

import base64
import hashlib
import hmac
import json
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from ..db import encode
from ..service import Fault


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Keys:
    def __init__(self, key):
        if len(key) != 32:
            raise ValueError("V2 key must be 32 random bytes")
        self.key = key

    def derive(self, purpose):
        return hmac.digest(self.key, purpose.encode(), "sha256")

    def seal(self, purpose, value):
        nonce = os.urandom(12)
        return b64(
            nonce
            + AESGCM(self.derive(purpose)).encrypt(
                nonce, encode(value).encode(), purpose.encode()
            )
        )

    def unseal(self, purpose, value):
        raw = unb64(value)
        return json.loads(
            AESGCM(self.derive(purpose)).decrypt(raw[:12], raw[12:], purpose.encode())
        )

    def sign(self, purpose, value):
        data = b64(encode(value).encode())
        return (
            data + "." + b64(hmac.digest(self.derive(purpose), data.encode(), "sha256"))
        )

    def verify(self, purpose, token, now):
        try:
            data, signature = token.split(".")
            if not hmac.compare_digest(
                hmac.digest(self.derive(purpose), data.encode(), "sha256"),
                unb64(signature),
            ):
                raise ValueError()
            value = json.loads(unb64(data))
            if value["expires"] <= now:
                raise Fault("expired_" + purpose, 410 if purpose == "cursor" else 401)
            return value
        except Fault:
            raise
        except Exception:
            raise Fault("invalid_" + purpose, 401) from None
