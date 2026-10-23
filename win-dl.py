#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Based on the public API workflow documented in pbatard/Fido.
# https://github.com/pbatard/Fido

import argparse
import hashlib
import os
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests

PROFILE = "606624d44113"
ORG_ID = "y6jn8c31"
INSTANCE_ID = "560dc9f3-1aa5-4a2f-b63c-9e18f8d0e175"

API = "https://www.microsoft.com/software-download-connector/api"
REFERER = "https://www.microsoft.com/software-download/windows11"

# IDs are taken from Fido's Windows 11 release table.
EDITION_IDS = [3813, 3816]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Referer": REFERER,
}


def request(session, url, **kwargs):
    r = session.get(url, timeout=30, **kwargs)
    r.raise_for_status()
    return r


def prepare_session(session, sid):
    print(f"[+] Initializing Microsoft session: {sid}")

    request(
        session,
        "https://vlscppe.microsoft.com/tags",
        params={"org_id": ORG_ID, "session_id": sid},
        allow_redirects=False,
    )

    r = request(
        session,
        "https://ov-df.microsoft.com/mdt.js",
        params={
            "instanceId": INSTANCE_ID,
            "PageId": "si",
            "session_id": sid,
        },
        allow_redirects=False,
    )

    w = re.search(r"[?&]w=([A-F0-9]+)", r.text)
    ticks = re.search(r'rticks\s*=\s*"\+?(\d+)', r.text)

    if not w or not ticks:
        raise RuntimeError(
            "Cannot parse Microsoft's session response. "
            "The API may have changed or the request was blocked."
        )

    request(
        session,
        "https://ov-df.microsoft.com/",
        params={
            "session_id": sid,
            "CustomerId": INSTANCE_ID,
            "PageId": "si",
            "w": w.group(1),
            "mdt": int(time.time() * 1000),
            "rticks": ticks.group(1),
        },
        allow_redirects=False,
    )


def api_json(session, endpoint, params):
    r = request(
        session,
        f"{API}/{endpoint}",
        params=params,
        headers=HEADERS,
    )
    data = r.json()

    errors = data.get("Errors") or []
    if errors:
        raise RuntimeError(
            f"Microsoft API error: {errors[0].get('Value', errors[0])}"
        )

    return data


def get_languages(locale):
    all_skus = []

    # Each product edition uses its own session ID.
    for edition_id in EDITION_IDS:
        session = requests.Session()
        session.headers.update(HEADERS)
        sid = str(uuid.uuid4())

        prepare_session(session, sid)

        params = {
            "profile": PROFILE,
            "productEditionId": edition_id,
            "SKU": "undefined",
            "friendlyFileName": "undefined",
            "Locale": locale,
            "sessionID": sid,
        }

        data = None
        for attempt in range(3):
            try:
                data = api_json(
                    session, "getskuinformationbyproductedition", params
                )
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2)

        for sku in data.get("Skus", []):
            all_skus.append({
                "language": sku.get("Language", ""),
                "display": sku.get("LocalizedLanguage", ""),
                "sku_id": sku.get("Id"),
                "session_id": sid,
                "session": session,
            })

    if not all_skus:
        raise RuntimeError("No languages/SKUs returned by Microsoft.")

    return all_skus


def get_download_links(sku):
    data = api_json(
        sku["session"],
        "GetProductDownloadLinksBySku",
        {
            "profile": PROFILE,
            "productEditionId": "undefined",
            "SKU": sku["sku_id"],
            "friendlyFileName": "undefined",
            "Locale": "en-us",
            "sessionID": sku["session_id"],
        },
    )

    links = []
    for item in data.get("ProductDownloadOptions", []):
        arch_type = item.get("DownloadType")
        arch = {0: "x86", 1: "x64", 2: "ARM64"}.get(
            int(arch_type), "unknown"
        )
        url = item.get("Uri")
        if url:
            links.append((arch, url))

    return links


def download(url, outdir):
    print(f"-->url:{url}");
    print(f"-->outdir:{outdir}");

    parsed = urlparse(url)
    filename = unquote(os.path.basename(parsed.path)) or "Windows11.iso"
    if not filename.lower().endswith(".iso"):
        filename = "Windows11.iso"

    path = outdir / filename
    partial_size = path.stat().st_size if path.exists() else 0

    headers = dict(HEADERS)
    if partial_size:
        headers["Range"] = f"bytes={partial_size}-"

    print(f"[+] Downloading: {filename}")
    print(f"[+] Destination: {path}")

    with requests.get(
        url, headers=headers, stream=True, timeout=(30, 120)
    ) as r:
        if partial_size and r.status_code == 206:
            mode = "ab"
        else:
            # Server ignored Range or returned a full response.
            mode = "wb"
            partial_size = 0

        r.raise_for_status()
        total = r.headers.get("Content-Length")
        total = int(total) + partial_size if total and mode == "ab" else (
            int(total) if total else 0
        )

        done = partial_size
        with open(path, mode) as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)

                if total:
                    pct = done * 100 / total
                    print(
                        f"\r{done / 1024**3:.2f} / "
                        f"{total / 1024**3:.2f} GiB ({pct:.1f}%)",
                        end="",
                        flush=True,
                    )

    print("\n[+] Calculating SHA-256...")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)

    digest = h.hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8"
    )

    print(f"[+] SHA-256: {digest}")
    print(f"[+] Saved: {path}")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch official Windows 11 ISO links and download."
    )
    parser.add_argument("--locale", default="zh-cn",
                        help="Locale, e.g. zh-cn or en-us")
    parser.add_argument("--lang", default="",
                        help="Language code, e.g. zh-cn or en-us")
    parser.add_argument("--arch", choices=["x64", "ARM64", "x86"],
                        default="x64")
    parser.add_argument("--out", default="./win11-iso")
    parser.add_argument("--list-languages", action="store_true")
    parser.add_argument(
            "--edition",
            choices=["multi"],
            default="multi",
            help="Windows edition bundle: Home/Pro/Edu",)

    args = parser.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    try:
        skus = get_languages(args.locale)

        languages = {}
        for sku in skus:
            languages.setdefault(sku["language"].lower(), []).append(sku)

        if args.list_languages:
            for code, entries in sorted(languages.items()):
                print(f"{code:12} {entries[0]['display']}")
            return

        lang = (args.lang or args.locale).lower()
        selected = languages.get(lang)

        if not selected:
            print("Available languages:")
            for code, entries in sorted(languages.items()):
                print(f"  {code:12} {entries[0]['display']}")
            raise RuntimeError(
                f"Language {lang!r} not found. "
                "Try --list-languages or choose --lang."
            )

        for sku in selected:
            links = get_download_links(sku)
            for arch, url in links:
                if arch == args.arch:
                    download(url, outdir)
                    return

        raise RuntimeError(
            f"No {args.arch} link found for language {lang}."
        )

    except requests.HTTPError as e:
        print(f"[HTTP ERROR] {e}", file=sys.stderr)
        if e.response is not None:
            print(
                f"Status: {e.response.status_code}\n"
                f"Response: {e.response.text[:1000]}",
                file=sys.stderr,
            )
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

