import json
from pathlib import Path

rules = json.loads(Path("tools/product_polish_replacements.json").read_text(encoding="utf-8"))
for name, pairs in rules.items():
    path = Path(name)
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if new in text:
            continue
        if old not in text:
            raise SystemExit(f"missing replacement anchor in {name}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
