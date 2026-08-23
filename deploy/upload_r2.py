"""Upload deploy/data/ to a Cloudflare R2 (S3-compatible) bucket (step 1).

Credentials come from the environment (R2 is S3 API compatible):

    R2_ACCOUNT_ID         - your Cloudflare account id
    R2_ACCESS_KEY_ID      - R2 API token key id
    R2_SECRET_ACCESS_KEY  - R2 API token secret
    R2_BUCKET             - bucket name (e.g. mf-holdings)

Usage::

    python deploy/upload_r2.py --dry-run          # list what would upload
    python deploy/upload_r2.py                    # upload everything
    python deploy/upload_r2.py --verify           # sha256-check objects after upload

Safe to re-run: identical (size+sha256) objects are skipped.
Manifest is uploaded last as ``manifest.json``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "deploy" / "data"
MANIFEST = ROOT / "deploy" / "manifest.json"


def load_env() -> None:
    """Read deploy/.env into the process env if set (does not override)."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v and not os.environ.get(k):
            os.environ[k] = v

ENDPOINT = "https://{account_id}.r2.cloudflarestorage.com"
CONTENT_TYPE = {
    ".json": "application/json",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".db": "application/octet-stream",
    ".pdf": "application/pdf",
}

# object key -> local path
def _index_bucket(bucket: str, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for obj in bucket.objects.all():
        if obj.key.startswith(prefix):
            out[obj.key] = ""
    return out


@dataclasses.dataclass
class Stat:
    size: int
    sha256: str


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--bucket-prefix", default="db",
                    help="object key prefix; empty string = bucket root")
    args = ap.parse_args()

    missing = [v for v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                           "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
               if not os.environ.get(v)]
    if missing:
        print("missing env:", ", ".join(missing))
        return 2

    if not STAGE.is_dir():
        print(f"staging dir missing: {STAGE} -- run deploy/prepare_data.py first")
        return 2

    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT.format(account_id=os.environ["R2_ACCOUNT_ID"]),
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"},
                      connect_timeout=30, read_timeout=120),
    )

    bucket = os.environ["R2_BUCKET"]
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        try:
            client.create_bucket(Bucket=bucket)
            print(f"bucket created: {bucket}")
        except ClientError as e:
            print(f"cannot create bucket {bucket}: {e}")
            return 2

    files = sorted(p for p in STAGE.rglob("*") if p.is_file())
    prefix = args.bucket_prefix.strip("/")

    existing = {}
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        obj = client.list_objects_v2(**kwargs)
        for o in obj.get("Contents", []):
            existing[o["Key"]] = (o["Size"], o.get("ETag", "").strip('"'))
        token = obj.get("NextContinuationToken")
        if not token:
            break

    to_upload = []
    for p in files:
        key = (prefix + "/" + p.relative_to(STAGE).as_posix()).lstrip("/")
        size = p.stat().st_size
        fmt = p.suffix.lower()
        ctype = CONTENT_TYPE.get(fmt, "application/octet-stream")
        if key in existing and existing[key][0] == size:
            stored = existing[key][1]
            if stored.startswith('"'):
                stored = stored.strip('"')
            # ETag may or may not be md5 (multipart is not); only trust quick skip
            # when it is exactly the md5 hex of the local file (small objects).
            if len(stored) == 32:
                local_md5 = hashlib.md5(p.read_bytes()).hexdigest()
                if local_md5 == stored:
                    continue
        to_upload.append((key, p, ctype))

    print(f"{len(files)} files total, {len(to_upload)} to upload, "
          f"{len(files) - len(to_upload)} unchanged")
    if args.dry_run:
        for key, _, _ in to_upload[:50]:
            print(f"  would upload: {key}")
        return 0

    def _up(item):
        """Upload one object with per-object retry + backoff (flaky links
        abort the whole batch otherwise)."""
        key, p, ctype = item
        last_err = None
        for attempt in range(4):
            try:
                client.upload_file(str(p), bucket, key,
                                   ExtraArgs={"ContentType": ctype})
                return key, p.stat().st_size, None
            except (BotoCoreError, ClientError, OSError) as e:
                last_err = e
                time.sleep(min(2 ** attempt * 2, 30))
        return key, p.stat().st_size, str(last_err)

    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    ok = fail = 0
    uploaded_bytes = 0
    failures: list[str] = []
    bar = (tqdm(total=len(to_upload), unit="obj", dynamic_ncols=True,
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} "
                           "[{elapsed}<{remaining}, {postfix}]")
           if tqdm else None)
    last_print = 0.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_up, it): it[0] for it in to_upload}
        for fut in concurrent.futures.as_completed(futs):
            key, nbytes, err = fut.result()
            if err:
                fail += 1
                failures.append(f"{key}: {err}")
            else:
                ok += 1
                uploaded_bytes += nbytes
            if bar is not None:
                bar.update(1)
                bar.set_postfix_str(f"{uploaded_bytes / 1e6:.0f} MB, "
                                    f"{fail} failed")
            elif time.time() - last_print >= 15:
                last_print = time.time()
                print(f"  progress: {ok + fail}/{len(to_upload)} "
                      f"({uploaded_bytes / 1e6:.0f} MB), {fail} failed",
                      flush=True)
    if bar is not None:
        bar.close()
    print(f"uploaded {ok} objects, {fail} failed")
    for f in failures[:10]:
        print(f"  FAILED {f}")
    if failures:
        print(f"  ({len(failures)} failed total — re-run to resume; "
              f"unchanged objects are skipped)")

    if args.verify:
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        mismatched = checked = 0
        vbar = (tqdm(total=len(m["files"]), unit="obj", dynamic_ncols=True)
                if tqdm else None)
        for ent in m["files"]:
            key = (prefix + "/" + ent["path"]).lstrip("/")
            resp = client.get_object(Bucket=bucket, Key=key)
            body = resp["Body"].read()
            got = (len(body), hashlib.sha256(body).hexdigest())
            checked += 1
            if got != (ent["size"], ent["sha256"]):
                mismatched += 1
                print(f"  MISMATCH {key}")
            if vbar is not None:
                vbar.update(1)
        if vbar is not None:
            vbar.close()
        print(f"verified {checked} objects, {mismatched} mismatched")

    manifest_key = (prefix + "/manifest.json").lstrip("/")
    client.upload_file(str(MANIFEST), bucket, manifest_key,
                       ExtraArgs={"ContentType": "application/json"})
    print(f"manifest uploaded: {manifest_key}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
