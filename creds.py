import json
import os
import sys

from dotenv import load_dotenv
from py_clob_client.client import ClobClient


def _normalize_private_key(raw: str) -> str:
    key = raw.strip()
    if key.startswith("0x"):
        key = key[2:]
    return key


def main() -> int:
    load_dotenv()

    raw_key = os.getenv("POLYMARKET_PK")
    if not raw_key:
        print("ERROR: POLYMARKET_PK not set in environment or .env")
        return 1

    private_key = _normalize_private_key(raw_key)
    if len(private_key) != 64:
        print("ERROR: POLYMARKET_PK must be a 64-hex private key (without 0x).")
        return 1

    client = ClobClient(
        host="https://clob.polymarket.com",
        key=private_key,
        chain_id=137,
        signature_type=1,  # EOA signature (MetaMask account)
    )

    creds = client.create_or_derive_api_creds()

    # Normalize ApiCreds object into a plain dict for printing.
    data = None
    if hasattr(creds, "model_dump"):
        data = creds.model_dump()
    elif hasattr(creds, "dict"):
        data = creds.dict()
    elif hasattr(creds, "__dict__"):
        data = dict(creds.__dict__)
    else:
        data = {}

    if not data:
        # Fallback: try common attribute names.
        data = {
            "apiKey": getattr(creds, "apiKey", None) or getattr(creds, "api_key", None),
            "secret": getattr(creds, "secret", None) or getattr(creds, "api_secret", None),
            "passphrase": getattr(creds, "passphrase", None) or getattr(creds, "api_passphrase", None),
        }

    print(json.dumps(data, indent=2))

    print("\nCopy into .env:")
    print(f"POLYMARKET_API_KEY={data.get('apiKey', '')}")
    print(f"POLYMARKET_API_SECRET={data.get('secret', '')}")
    print(f"POLYMARKET_PASSPHRASE={data.get('passphrase', '')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())