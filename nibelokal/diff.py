"""Compare two backup snapshots. Answers "what did I change since June?"."""
from __future__ import annotations

import json


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def print_diff(old_path: str, new_path: str) -> int:
    old, new = load(old_path), load(new_path)
    a = {r["address"]: r for r in old["registers"] if "error" not in r}
    b = {r["address"]: r for r in new["registers"] if "error" not in r}

    print("old: %s  (%s)" % (old_path, old.get("taken_at")))
    print("new: %s  (%s)" % (new_path, new.get("taken_at")))
    print()

    changed = []
    for address in sorted(set(a) & set(b)):
        if not b[address].get("writable"):
            continue                      # measurements change constantly; settings do not
        if str(a[address].get("value")) != str(b[address].get("value")):
            changed.append((address, a[address], b[address]))

    if not changed:
        print("No settings differ between these two snapshots.")
    else:
        print("%d settings changed:" % len(changed))
        for address, was, now in changed:
            print("  %-6d %-44s %s -> %s %s"
                  % (address, (now.get("title") or "")[:44],
                     was.get("value"), now.get("value"), now.get("unit") or ""))

    only_new = sorted(set(b) - set(a))
    only_old = sorted(set(a) - set(b))
    if only_new:
        print("\n%d registers readable only in the new snapshot: %s"
              % (len(only_new), ", ".join(str(x) for x in only_new[:20])))
    if only_old:
        print("\n%d registers readable only in the old snapshot: %s"
              % (len(only_old), ", ".join(str(x) for x in only_old[:20])))
    return 0
