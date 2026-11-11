"""Return the administrator entry over a private SSH pipe. Never log stdout."""

import json
import os
import sys
from pathlib import Path

from cloud_memory_hub import admin_credentials
from cloud_memory_hub.config import read_private_file
from cloud_memory_hub.onboarding.connect import valid_origin


def main():
    try:
        origin = valid_origin(os.environ["CMH_PUBLIC_ORIGIN"])
        if not origin.startswith("https://"):
            return 1
        directory = Path(os.environ.get("CMH_CONFIG_DIR", "/config"))
        token = read_private_file(directory / "admin-token").decode("ascii").strip()
        proof_for = getattr(admin_credentials, "initial_admin_setup_proof", None)
        proof = proof_for(token) if proof_for else None
        result = {"origin": origin}
        if proof is not None:
            result["proof"] = proof
        elif sys.argv[1:] != ["--setup-only"]:
            result["credential"] = token
        print(json.dumps(result))
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
