import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from src.financial_statements import refresh_stale
    for i in range(0, 80):
        batch = refresh_stale(limit=12)
        print(json.dumps({"batch": i, "n": len(batch)}), flush=True)
        if not batch:
            break
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
