"""Download a file for an image build from the first of its sources that
gives it with the expected checksum.

    python3 fetch.py --sha256 <hex> [--sha256 <hex>] --out <path> <url> [<url> ...]

The checksum makes every source equally safe: a file that does not match is
deleted and the next source is tried, so the result can be no other file.
A publisher that builds a release twice, such as deno on GitHub and on its
own server, gives two checksums, each from its own source.
Only the Python standard library.
"""

import argparse
import hashlib
import os
import sys
import urllib.request

TIMEOUT = 60
CHUNK = 1 << 20


def download(url: str, out: str) -> str:
    """Save url at out and return the file's sha256."""
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "piano-transcription-build"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response, open(out, "wb") as file:
        while chunk := response.read(CHUNK):
            digest.update(chunk)
            file.write(chunk)
    return digest.hexdigest()


def fetch(urls: list[str], sha256: str | list[str], out: str, download=download) -> str | None:
    """The source that gave the file, or None when none did."""
    accepted = {checksum.lower() for checksum in ([sha256] if isinstance(sha256, str) else sha256)}
    partial = out + ".part"
    for url in urls:
        try:
            got = download(url, partial)
        except Exception as error:
            print(f"fetch: {url} failed: {error}", file=sys.stderr)
        else:
            if got in accepted:
                os.replace(partial, out)
                return url
            print(f"fetch: {url} gave a file with sha256 {got}, not {' or '.join(sorted(accepted))}", file=sys.stderr)
        if os.path.exists(partial):
            os.remove(partial)
        print("fetch: trying the next source", file=sys.stderr)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sha256", required=True, action="append")
    parser.add_argument("--out", required=True)
    parser.add_argument("urls", nargs="+")
    args = parser.parse_args(argv)
    source = fetch(args.urls, args.sha256, args.out)
    if source is None:
        print(f"fetch: no source gave {os.path.basename(args.out)} with sha256 {' or '.join(args.sha256)}", file=sys.stderr)
        return 1
    print(f"fetch: {args.out} from {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
