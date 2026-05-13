#!/usr/bin/env python3
"""Generate an ACME account key in certbot JWK format.

Usage: python3 generate_acme_key.py <output-path>
"""

import base64
import json
import sys

from cryptography.hazmat.primitives.asymmetric import rsa


def _b64(n: int, length: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(length, byteorder="big")).rstrip(b"=").decode()


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <output-path>", file=sys.stderr)
        sys.exit(1)

    output_path = sys.argv[1]

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = key.private_numbers()
    pub = nums.public_numbers

    jwk = {
        "kty": "RSA",
        "n": _b64(pub.n, 256),
        "e": _b64(pub.e, 3),
        "d": _b64(nums.d, 256),
        "p": _b64(nums.p, 128),
        "q": _b64(nums.q, 128),
        "dp": _b64(nums.dmp1, 128),
        "dq": _b64(nums.dmq1, 128),
        "qi": _b64(nums.iqmp, 128),
    }

    with open(output_path, "w") as f:
        json.dump(jwk, f, indent=2)

    print(f"Generated ACME account key: {output_path}")


if __name__ == "__main__":
    main()
