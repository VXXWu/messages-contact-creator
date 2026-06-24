#!/usr/bin/env python3
"""
Fetch and normalize the first/last name datasets used for name recognition.
Writes data/first_names.txt and data/last_names.txt (lowercased, deduped).

Source: smashew/NameDatabases (real-people first names + surnames).
Run once; the tool reads the cached files. Re-run to refresh or swap sources.
"""
import re
import sys
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
SOURCES = {
    "first_names.txt": "https://raw.githubusercontent.com/smashew/NameDatabases/master/NamesDatabases/first%20names/all.txt",
    "last_names.txt": "https://raw.githubusercontent.com/smashew/NameDatabases/master/NamesDatabases/surnames/all.txt",
}
# letters (incl. accents), hyphen, apostrophe; <=40 chars
VALID = re.compile(r"[a-zà-ɏ'’\-]+")


def normalize(blob):
    out = set()
    for line in blob.splitlines():
        w = line.strip().lower()
        if w and len(w) <= 40 and VALID.fullmatch(w):
            out.add(w)
    return out


def main():
    DATA.mkdir(exist_ok=True)
    for fname, url in SOURCES.items():
        print(f"fetching {fname} …")
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                blob = r.read().decode("utf-8-sig", errors="ignore")
        except Exception as e:
            sys.exit(f"failed to fetch {url}: {e}")
        names = sorted(normalize(blob))
        (DATA / fname).write_text("\n".join(names))
        print(f"  wrote {len(names)} -> {DATA / fname}")


if __name__ == "__main__":
    main()
