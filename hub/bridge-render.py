#!/usr/bin/env python3
"""bridge-render — minimal reference renderer for AI Bridge reading pages.

Renders bridge letters (.md) to sibling .html files so the operator can
read them in a browser behind the read-only web site (see the protocol,
section 9). Deliberately small: standard library only, a Markdown subset
(headings, paragraphs, bold, inline code, code fences, links, lists,
blockquotes, hr). The hub calls the renderer configured in its
RENDER_SH path whenever a letter lands; deployers may replace this file
with a fuller Markdown engine without changing anything else.

Usage:
  bridge-render.py <file.md>      render one file
  bridge-render.py --all [ROOT]   render every .md under inbox/, shared/,
                                  archive/ of ROOT (default /srv/bridge,
                                  or $BRIDGE_ROOT)
Output: <file>.html next to each .md (only rewritten when the .md is newer).
"""
import html
import os
import re
import sys

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:46em;margin:2em auto;
padding:0 1em;line-height:1.65}}pre{{background:#f4f4f4;padding:.8em;overflow:auto}}
code{{background:#f4f4f4;padding:.1em .3em}}pre code{{padding:0}}
blockquote{{border-left:3px solid #ccc;margin-left:0;padding-left:1em;color:#555}}
</style></head><body>
{body}
</body></html>
"""


def inline(text):
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def render_md(md):
    out, in_code, code_buf, in_list = [], False, [], False
    for line in md.splitlines():
        if line.strip().startswith("```"):
            if in_code:
                out.append("<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>")
                code_buf, in_code = [], False
            else:
                in_code = True
            continue
        if in_code:
            code_buf.append(line)
            continue
        s = line.strip()
        if s.startswith("- ") or s.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + inline(s[2:]) + "</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not s:
            continue
        m = re.match(r"(#{1,6})\s+(.*)", s)
        if m:
            lvl = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lvl, inline(m.group(2)), lvl))
        elif s in ("---", "***"):
            out.append("<hr>")
        elif s.startswith("> "):
            out.append("<blockquote>" + inline(s[2:]) + "</blockquote>")
        else:
            out.append("<p>" + inline(s) + "</p>")
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>")
    return "\n".join(out)


def render_file(path):
    md_path = os.path.abspath(path)
    html_path = md_path[:-3] + ".html" if md_path.endswith(".md") else md_path + ".html"
    if os.path.exists(html_path) and os.path.getmtime(html_path) >= os.path.getmtime(md_path):
        return False
    with open(md_path, encoding="utf-8") as f:
        md = f.read()
    title = os.path.basename(md_path)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(PAGE.format(title=html.escape(title), body=render_md(md)))
    return True


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    if args[0] == "--all":
        root = args[1] if len(args) > 1 else os.environ.get("BRIDGE_ROOT", "/srv/bridge")
        n = 0
        for sub in ("inbox", "shared", "archive"):
            for dirpath, _dirs, files in os.walk(os.path.join(root, sub)):
                for name in files:
                    if name.endswith(".md") and render_file(os.path.join(dirpath, name)):
                        n += 1
        print("rendered %d file(s)" % n)
        return 0
    render_file(args[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
