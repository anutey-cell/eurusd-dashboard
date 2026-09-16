from __future__ import annotations

import pathlib


def replace_once(path: str, old: str, new: str) -> None:
    p = pathlib.Path(path)
    text = p.read_text(encoding="utf-8")
    if old not in text or text.count(old) != 1:
        raise SystemExit(f"patch anchor missing/non-unique: {path}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# SQLite/tests and some migrations can expose DateTime columns as ISO strings
# with a T separator. Normalize with fromisoformat before legacy strptime.
p = "backend/services/data_freshness.py"
replace_once(
    p,
    '''    if isinstance(ts, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                ts = datetime.strptime(ts.split("+")[0], fmt); break
            except ValueError:
                continue
''',
    '''    if isinstance(ts, str):
        raw = ts
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    ts = datetime.strptime(raw.split("+")[0], fmt); break
                except ValueError:
                    continue
''',
)

p = "backend/services/canonical_market_data.py"
replace_once(
    p,
    '''        if isinstance(ts, str):
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    ts = datetime.strptime(ts.split("+")[0], fmt); break
                except ValueError:
                    continue
''',
    '''        if isinstance(ts, str):
            raw = ts
            try:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                    try:
                        ts = datetime.strptime(raw.split("+")[0], fmt); break
                    except ValueError:
                        continue
''',
)

print("C0 ISO timestamp normalization applied")
