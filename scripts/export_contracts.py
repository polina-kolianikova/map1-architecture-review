"""Generate or verify the static OpenAPI document from this sample only."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.main import app

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = ROOT / "docs/contracts/openapi.json"
    expected = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not target.exists() or target.read_text() != expected:
            raise SystemExit("Static OpenAPI does not match the sample runtime.")
        print("Static OpenAPI matches the sample runtime.")
    else:
        target.write_text(expected)
        print("Wrote docs/contracts/openapi.json")
