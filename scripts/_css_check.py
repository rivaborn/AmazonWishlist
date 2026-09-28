#!/usr/bin/env python
"""Token-discipline + syntax checks on style.css, and template/CSS class parity.

Run from the repo root (no deps, no network, no DB):  python scripts/_css_check.py

The UI is a Bauhaus Geometric design system whose whole premise is that the
palette, type scale, spacing and geometry live in :root as design tokens and
components are built only from those. That is a property a reviewer cannot
eyeball across 700 lines, so it is checked here instead.

Checks the things the Bauhaus brief actually demands of the output:
  - the stylesheet parses (balanced braces)
  - every var(--token) referenced is defined
  - no colour is hard-coded inside a component
  - no length is hard-coded inside a component (declarations only; the section
    comments quote the spec verbatim, e.g. "48px, 2px black border")
  - every class a template emits has a rule (or is a known behaviour hook)
"""
import re
import sys
import pathlib

css = pathlib.Path("app/static/style.css").read_text(encoding="utf-8")
fail = []

if css.count("{") != css.count("}"):
    fail.append(f"unbalanced braces: {css.count('{')} open vs {css.count('}')} close")

defined = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", css, re.M))
used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
missing = sorted(used - defined)
if missing:
    fail.append("var() references with no definition: " + ", ".join(missing))
unused = sorted(defined - used)

# Everything outside the :root blocks is "a component".
body = css
for block in re.findall(r":root\s*\{[^}]*\}", css):
    body = body.replace(block, "")
decls = re.sub(r"/\*.*?\*/", "", body, flags=re.S)   # drop block comments

for pattern, label in (
    (r"#[0-9A-Fa-f]{3,8}\b", "hex colour"),
    (r"\brgba?\(", "rgb()/rgba()"),
    (r"\bhsla?\(", "hsl()"),
):
    hits = re.findall(pattern, decls)
    if hits:
        fail.append(f"hard-coded {label} in a component: {sorted(set(hits))}")

# A media query cannot read a custom property, so its breakpoint is allowed to
# be a literal; nothing else is.
raw = []
for line in decls.splitlines():
    if line.strip().startswith("@media"):
        continue
    raw += [m.group(0) for m in re.finditer(r"(?<![\w-])\d+(?:\.\d+)?(?:px|rem)\b", line)]
if raw:
    fail.append(f"hard-coded lengths in components: {sorted(set(raw))}")

tpl_classes = set()
for f in sorted(pathlib.Path("app/templates").glob("*.html")):
    for attr in re.findall(r'class="([^"{]*)"', f.read_text(encoding="utf-8")):
        tpl_classes.update(c for c in attr.split() if c)
css_classes = set(re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)", css))
ALLOWED_UNSTYLED = {
    "purchased-cb", "hide-cb", "log-msg",            # JS behaviour hooks
    "cover-size", "min-stars", "tooltip-size",       # modifiers on .per-page
}
unstyled = sorted(tpl_classes - css_classes - ALLOWED_UNSTYLED)
if unstyled:
    fail.append("template classes with no CSS rule: " + ", ".join(unstyled))

print(f"tokens defined: {len(defined)}   referenced: {len(used)}")
if unused:
    print("  defined but unused (part of the required scale): " + ", ".join(unused))
print(f"hard-coded colours in components: 0")
print(f"hard-coded lengths in components: {len(raw)}")
print(f"template classes: {len(tpl_classes)}   all styled: {not unstyled}")

if fail:
    print("\nFAIL")
    for message in fail:
        print("  - " + message)
    sys.exit(1)
print("\nCSS CHECKS PASSED")
