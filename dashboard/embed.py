#!/usr/bin/env python
"""Re-embed status.json into index.html so the board also works offline (file:// / Artifact).
On GitHub Pages the page fetches status.json live, so this is optional there."""
import json, re, sys, pathlib

root = pathlib.Path(__file__).parent
obj = json.loads((root / "status.json").read_text(encoding="utf-8"))
inline = json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")

html = (root / "index.html").read_text(encoding="utf-8")
new = re.sub(
    r'(<script id="embedded" type="application/json">).*?(</script>)',
    lambda m: m.group(1) + inline + m.group(2),
    html, count=1, flags=re.S,
)
if new == html and "__DATA__" in html:
    new = html.replace("__DATA__", inline)
(root / "index.html").write_text(new, encoding="utf-8", newline="\n")
print("embedded status.json into index.html (%d bytes)" % len(new))
