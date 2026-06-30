#!/usr/bin/env python3
"""Build the static 'Mutator Lever Manuals' site from the 7 CLAUDE.md files.

No framework, no network/CDN. Renders a curated subset of Markdown (headings,
tables, lists, blockquotes, code spans, bold/italic, hr) to clean styled HTML at
build time, so the pages are self-contained and look good offline.

Run:  python3 build.py   (writes index.html + levers/*.html + assets/style.css)
"""

import html
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "claude_mds"
OUT = HERE
LEVERS_DIR = OUT / "levers"
ASSETS = OUT / "assets"

# ---- Lever registry: order, file, short name, one-line thesis, accent --------
# Theses are distilled from the catalog (outer-loop-lever-catalog.md).
LEVERS = [
    {
        "slug": "lever1-doc-identity-retrieval",
        "num": "1",
        "name": "Document-identity retrieval & grounding",
        "thesis": "The #1 long-document failure is grounding a citation in the WRONG source document — "
                  "make retrieval carry document identity and search inside the text.",
        "confidence": "HIGH",
        "objective": "Cut the wrong-document grounding rate",
        "gate": ">1 candidate document OR a defined-term-heavy query",
        "metric": "Document-Level Retrieval Mismatch + citation precision/recall vs offsets",
    },
    {
        "slug": "lever2-citation-faithfulness",
        "num": "2",
        "name": "Citation / quote faithfulness",
        "thesis": "Legal citations are frequently fabricated or post-rationalized — re-verify every cited "
                  "clause against a re-fetched source span before finalize.",
        "confidence": "HIGH",
        "objective": "Make citations and quotes faithful",
        "gate": "Citation/quote count > 0 (skip pure narrative)",
        "metric": "Citation precision/recall vs offsets; cited-but-unused rate; temporal/jurisdiction errors",
    },
    {
        "slug": "lever3-verify-before-finalize",
        "num": "3",
        "name": "Verify-before-finalize self-critique",
        "thesis": "A model can catch its own errors — but only if the re-check is INDEPENDENT of the draft "
                  "and GROUNDED in re-fetched sources.",
        "confidence": "HIGH",
        "objective": "Re-check the draft independently and against sources",
        "gate": "Output risk: long context, multi-criterion rubric, or low confidence",
        "metric": "# claims changed + fraction of changes corroborated by a source re-fetch",
    },
    {
        "slug": "lever4-omission-coverage",
        "num": "4",
        "name": "Omission / coverage of missing text",
        "thesis": "Flagging risk from the ABSENCE of a clause is a distinct skill models are bad at "
                  "(~50% vs ~90%) — sweep a document-type checklist for what's missing.",
        "confidence": "HIGH",
        "objective": "Flag clauses/protections that should be present but aren't",
        "gate": "Detected document type / task class",
        "metric": "Omission-detection F1/recall + false-positive 'missing' rate",
    },
    {
        "slug": "lever5-plan-decomposition",
        "num": "5",
        "name": "Plan + checklist decomposition",
        "thesis": "Multi-criterion work loses points to SKIPPED subtasks — emit a plan of discrete subtasks "
                  "and a rubric-aligned checklist so no criterion is silently dropped.",
        "confidence": "HIGH (scope: missing-step / coverage)",
        "objective": "Stop skipping subtasks; expose prose-hidden coverage gaps",
        "gate": "Criteria / clause count over a threshold",
        "metric": "Per-criterion pass rate + missing-step rate",
    },
    {
        "slug": "lever6-targeted-reposition",
        "num": "6",
        "name": "Targeted re-read / re-positioning",
        "thesis": "Buried mid-context clauses are a positional effect, not lost information — re-fetch the "
                  "span and re-present it first/last, don't re-read everything.",
        "confidence": "MEDIUM (overlaps the eviction lever)",
        "objective": "Recover buried spans by re-positioning",
        "gate": "(long context) AND (a runtime uncertainty signal)",
        "metric": "Recall of target clauses by position (middle vs ends)",
    },
]

ALL_LEVERS = {
    "slug": "all-levers-accuracy",
    "name": "All levers — general accuracy manual",
    "thesis": "One disciplined 'improve accuracy' manual spanning all six families, with GROUNDED > "
              "UNGROUNDED as the headline anti-overfit rule.",
    "objective": "Raise held-out accuracy across all six families, at no worse cost",
    "gate": "Each family keeps its own runtime gate + intermediate metric",
    "metric": "The relevant family's intermediate metric, tracked alongside the rubric",
}

# ============================ tiny markdown renderer ==========================

INLINE_CODE = re.compile(r"`([^`]+)`")
# Bold: allow inner '*' so a bold span may itself contain an *italic* run.
BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
# Italic: a single '*' not part of a '**', not glued to a word char (so we don't
# eat apostrophes-as-asterisks or stray multiplication signs).
ITALIC = re.compile(r"(?<![\*\w])\*(?!\s)([^*\n]+?)\*(?![\*\w])")


def _apply_italic(text: str) -> str:
    return ITALIC.sub(r"<em>\1</em>", text)


def render_inline(text: str) -> str:
    """Escape, then apply inline code / bold / italic. Code wins (never nest into it);
    bold may contain italic (process bold first, recurse italic into its content)."""
    placeholders = []

    def stash_code(m):
        placeholders.append(html.escape(m.group(1)))
        return f"\x00{len(placeholders)-1}\x00"

    text = INLINE_CODE.sub(stash_code, text)
    text = html.escape(text)

    def bold_sub(m):
        return "<strong>" + _apply_italic(m.group(1)) + "</strong>"

    text = BOLD.sub(bold_sub, text)
    text = _apply_italic(text)

    def restore(m):
        return f"<code>{placeholders[int(m.group(1))]}</code>"

    text = re.sub(r"\x00(\d+)\x00", restore, text)
    return text


def split_table_row(line: str):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def render_markdown(md: str) -> str:
    lines = md.split("\n")
    out = []
    i = 0
    n = len(lines)

    def flush_para(buf):
        if buf:
            out.append("<p>" + render_inline(" ".join(buf)) + "</p>")
            buf.clear()

    para: list[str] = []

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # blank line
        if not stripped:
            flush_para(para)
            i += 1
            continue

        # headings
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para(para)
            level = len(m.group(1))
            out.append(f"<h{level}>{render_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # horizontal rule
        if re.match(r"^---+$", stripped):
            flush_para(para)
            out.append("<hr>")
            i += 1
            continue

        # table (a header row followed by a |---|---| separator)
        if stripped.startswith("|") and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]) and "-" in lines[i + 1]:
            flush_para(para)
            header = split_table_row(stripped)
            i += 2  # skip header + separator
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(split_table_row(lines[i].strip()))
                i += 1
            thead = "".join(f"<th>{render_inline(c)}</th>" for c in header)
            body = ""
            for r in rows:
                cells = "".join(f"<td>{render_inline(c)}</td>" for c in r)
                body += f"<tr>{cells}</tr>"
            out.append(
                f'<div class="table-wrap"><table><thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table></div>'
            )
            continue

        # blockquote (possibly multi-line, with blank-line-less continuation)
        if stripped.startswith(">"):
            flush_para(para)
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                content = re.sub(r"^\s*>\s?", "", lines[i])
                buf.append(content)
                i += 1
            # render inner block (supports paragraphs separated by empty '>' lines)
            inner_md = "\n".join(buf)
            inner_html = render_markdown(inner_md)
            out.append(f"<blockquote>{inner_html}</blockquote>")
            continue

        # lists (unordered - or *, ordered 1.) with wrapped continuation lines
        list_m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if list_m:
            flush_para(para)
            ordered = bool(re.match(r"^\d+\.$", list_m.group(2)))
            tag = "ol" if ordered else "ul"
            items = []
            while i < n:
                lm = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[i])
                if lm:
                    item_lines = [lm.group(3)]
                    i += 1
                    # consume wrapped continuation lines (indented, not a new bullet, not blank)
                    while i < n and lines[i].strip() and not re.match(r"^(\s*)([-*]|\d+\.)\s+", lines[i]):
                        if lines[i].startswith(" ") or lines[i].startswith("\t"):
                            item_lines.append(lines[i].strip())
                            i += 1
                        else:
                            break
                    items.append(" ".join(item_lines))
                elif not lines[i].strip():
                    # blank line: peek — if next is another list item, continue the list
                    j = i + 1
                    if j < n and re.match(r"^(\s*)([-*]|\d+\.)\s+", lines[j]):
                        i += 1
                        continue
                    break
                else:
                    break
            lis = "".join(f"<li>{render_inline(it)}</li>" for it in items)
            out.append(f"<{tag}>{lis}</{tag}>")
            continue

        # default: paragraph text (accumulate wrapped lines)
        para.append(stripped)
        i += 1

    flush_para(para)
    return "\n".join(out)


# =============================== HTML scaffolding =============================

def page(title: str, body: str, depth: int) -> str:
    css = ("assets/style.css" if depth == 0 else "../assets/style.css")
    home = ("index.html" if depth == 0 else "../index.html")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="{css}">
</head>
<body>
<header class="topbar">
  <a class="brand" href="{home}">Mutator&nbsp;·&nbsp;Lever&nbsp;Manuals</a>
  <span class="subtle">operating manuals for the Harvey-harness mutator</span>
</header>
<main>
{body}
</main>
<footer class="foot">
  <span>Mutator operating-manual set — accuracy levers. Methodology only (no benchmark task/rubric data).</span>
</footer>
</body>
</html>
"""


def lever_card(l: dict, is_all: bool = False) -> str:
    href = f"levers/{l['slug']}.html"
    badge = "ALL" if is_all else l["num"]
    conf = l.get("confidence", "")
    conf_html = f'<span class="conf">{html.escape(conf)}</span>' if conf else ""
    kls = "card all" if is_all else "card"
    name = l["name"]
    return f"""<a class="{kls}" href="{href}">
  <div class="card-head"><span class="badge">{badge}</span><h3>{html.escape(name)}</h3>{conf_html}</div>
  <p class="thesis">{html.escape(l['thesis'])}</p>
  <span class="card-cta">Read the CLAUDE.md &rarr;</span>
</a>"""


def build_index() -> str:
    intro = """
<section class="hero">
  <h1>Mutator lever manuals</h1>
  <p class="lede">An outer-loop <strong>mutator</strong> (an Opus agent) edits a frozen Harvey legal-agent
  harness to make it better, scored on a held-out dev gate. Its operating manual is a <code>CLAUDE.md</code>.
  Below are <strong>seven</strong> manuals: one per accuracy lever from the deep-research catalog, plus a
  general all-levers manual. Each retargets the same hard-won protocol at a different accuracy goal — and in
  every one, <strong>accuracy is the objective while cost (and no thrash, no turn-runaway) is the hard gate.</strong></p>
</section>
"""
    cards = "\n".join(lever_card(l) for l in LEVERS)
    all_card = lever_card(ALL_LEVERS, is_all=True)
    grid = f"""
<section>
  <h2 class="section-title">The levers</h2>
  <div class="grid">
{cards}
  </div>
</section>
<section>
  <h2 class="section-title">Across all levers</h2>
  <div class="grid one">
{all_card}
  </div>
</section>
"""
    return page("Mutator lever manuals", intro + grid, depth=0)


def meta_strip(l: dict) -> str:
    rows = [
        ("Objective", l.get("objective", "")),
        ("Runtime gate", l.get("gate", "")),
        ("Intermediate metric", l.get("metric", "")),
    ]
    if l.get("confidence"):
        rows.insert(0, ("Confidence", l["confidence"]))
    items = "".join(
        f'<div class="meta-item"><span class="meta-k">{html.escape(k)}</span>'
        f'<span class="meta-v">{html.escape(v)}</span></div>'
        for k, v in rows if v
    )
    return f'<div class="meta-strip">{items}</div>'


def build_lever_page(l: dict, is_all: bool = False) -> str:
    md = (SRC / f"{l['slug']}.CLAUDE.md").read_text(encoding="utf-8")
    rendered = render_markdown(md)
    badge = "ALL" if is_all else l["num"]
    head = f"""
<nav class="crumbs"><a href="../index.html">All levers</a> <span>/</span> <span>{html.escape(l['name'])}</span></nav>
<div class="lever-head">
  <span class="badge big">{badge}</span>
  <div>
    <h1>{html.escape(l['name'])}</h1>
    <p class="thesis lever-thesis">{html.escape(l['thesis'])}</p>
  </div>
</div>
{meta_strip(l)}
<div class="doc-meta">Operating manual &mdash; <code>{l['slug']}.CLAUDE.md</code></div>
"""
    doc = f'<article class="doc">{rendered}</article>'
    return page(l["name"], head + doc, depth=1)


CSS = """:root{
  --bg:#0f1115; --panel:#171a21; --panel2:#1d2129; --ink:#e8eaed; --muted:#9aa3af;
  --line:#2a2f3a; --accent:#6ea8fe; --accent2:#8b9cff; --code-bg:#11141a; --code-ink:#cfe3ff;
  --good:#5bd6a0; --warn:#e9c46a; --maxw:1080px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
a{color:var(--accent); text-decoration:none}
a:hover{text-decoration:underline}
code{
  font-family:"SF Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.875em; background:var(--code-bg); color:var(--code-ink);
  padding:.12em .4em; border-radius:5px; border:1px solid var(--line);
}
.topbar{
  position:sticky; top:0; z-index:5; display:flex; align-items:baseline; gap:14px; flex-wrap:wrap;
  padding:13px 24px; background:rgba(15,17,21,.86); backdrop-filter:saturate(140%) blur(8px);
  border-bottom:1px solid var(--line);
}
.brand{font-weight:700; letter-spacing:.2px; color:var(--ink)}
.brand:hover{text-decoration:none; color:var(--accent)}
.subtle{color:var(--muted); font-size:.82rem}
main{max-width:var(--maxw); margin:0 auto; padding:0 24px 64px}
.foot{max-width:var(--maxw); margin:0 auto; padding:28px 24px 56px; color:var(--muted); font-size:.82rem; border-top:1px solid var(--line)}

/* hero / index */
.hero{padding:52px 0 30px; border-bottom:1px solid var(--line); margin-bottom:34px}
.hero h1{font-size:2.5rem; line-height:1.1; margin:0 0 16px; letter-spacing:-.5px}
.lede{font-size:1.075rem; color:#c7ccd6; max-width:78ch; margin:0}
.section-title{font-size:.82rem; text-transform:uppercase; letter-spacing:.16em; color:var(--muted); margin:38px 0 16px; font-weight:600}
.grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(330px,1fr)); gap:16px}
.grid.one{grid-template-columns:1fr}
.card{
  display:flex; flex-direction:column; gap:10px; padding:20px 20px 16px;
  background:linear-gradient(180deg,var(--panel),var(--panel2)); border:1px solid var(--line);
  border-radius:14px; color:var(--ink); transition:border-color .15s, transform .15s, box-shadow .15s;
}
.card:hover{text-decoration:none; transform:translateY(-2px);
  border-color:var(--accent); box-shadow:0 8px 30px -16px rgba(110,168,254,.5)}
.card.all{background:linear-gradient(180deg,#191d2b,#1a2030); border-color:#33405e}
.card-head{display:flex; align-items:center; gap:11px}
.card-head h3{margin:0; font-size:1.06rem; line-height:1.25; flex:1}
.badge{
  flex:none; width:26px; height:26px; display:grid; place-items:center; border-radius:8px;
  background:#243044; color:var(--accent); font-weight:700; font-size:.85rem; border:1px solid #31405a;
}
.badge.big{width:46px; height:46px; font-size:1.15rem; border-radius:12px}
.conf{font-size:.62rem; letter-spacing:.1em; font-weight:700; color:#0f1115; background:var(--good);
  padding:.2em .55em; border-radius:999px; text-transform:uppercase; white-space:nowrap}
.thesis{margin:0; color:#bcc3cf; font-size:.94rem; line-height:1.55}
.card-cta{margin-top:auto; color:var(--accent); font-size:.85rem; font-weight:600}

/* lever page header */
.crumbs{padding:22px 0 0; color:var(--muted); font-size:.85rem}
.crumbs span{margin:0 6px; color:var(--line)}
.lever-head{display:flex; gap:18px; align-items:flex-start; padding:16px 0 8px}
.lever-head h1{margin:0 0 8px; font-size:2rem; letter-spacing:-.4px; line-height:1.12}
.lever-thesis{font-size:1.02rem; color:#c7ccd6; max-width:80ch}
.meta-strip{display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:1px;
  background:var(--line); border:1px solid var(--line); border-radius:12px; overflow:hidden; margin:18px 0 8px}
.meta-item{background:var(--panel); padding:12px 14px; display:flex; flex-direction:column; gap:3px}
.meta-k{font-size:.66rem; text-transform:uppercase; letter-spacing:.13em; color:var(--muted); font-weight:600}
.meta-v{font-size:.9rem; color:#dfe3ea}
.doc-meta{color:var(--muted); font-size:.8rem; margin:14px 0 0}

/* the rendered manual */
.doc{
  margin-top:18px; padding:30px 34px; background:var(--panel); border:1px solid var(--line);
  border-radius:16px;
}
.doc h1{font-size:1.55rem; margin:0 0 6px; letter-spacing:-.3px}
.doc h2{font-size:1.22rem; margin:34px 0 12px; padding-top:18px; border-top:1px solid var(--line); letter-spacing:-.2px}
.doc h2:first-of-type{border-top:0; padding-top:0; margin-top:6px}
.doc h3{font-size:1.02rem; margin:24px 0 10px}
.doc p{margin:12px 0; color:#d5dae2}
.doc ul,.doc ol{margin:12px 0; padding-left:24px}
.doc li{margin:7px 0; color:#d5dae2}
.doc li::marker{color:var(--accent)}
.doc strong{color:#fff; font-weight:650}
.doc em{color:#e4d9b8; font-style:italic}
.doc hr{border:0; border-top:1px solid var(--line); margin:26px 0}
.doc blockquote{
  margin:18px 0; padding:6px 18px; border-left:3px solid var(--accent2);
  background:var(--panel2); border-radius:0 10px 10px 0;
}
.doc blockquote p{color:#cdd3dd}
.table-wrap{overflow-x:auto; margin:16px 0; border:1px solid var(--line); border-radius:12px}
.doc table{border-collapse:collapse; width:100%; font-size:.92rem}
.doc th,.doc td{text-align:left; padding:11px 14px; border-bottom:1px solid var(--line); vertical-align:top}
.doc th{background:var(--panel2); color:#eef1f6; font-weight:650; white-space:nowrap}
.doc tr:last-child td{border-bottom:0}
.doc td code,.doc th code{white-space:nowrap}

@media (max-width:560px){
  .hero h1{font-size:2rem}
  .lever-head{flex-direction:column; gap:10px}
  .doc{padding:22px 18px}
}
"""


def main():
    LEVERS_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    (ASSETS / "style.css").write_text(CSS, encoding="utf-8")
    (OUT / "index.html").write_text(build_index(), encoding="utf-8")
    for l in LEVERS:
        (LEVERS_DIR / f"{l['slug']}.html").write_text(build_lever_page(l), encoding="utf-8")
    (LEVERS_DIR / f"{ALL_LEVERS['slug']}.html").write_text(
        build_lever_page(ALL_LEVERS, is_all=True), encoding="utf-8"
    )
    print("Built:")
    print("  index.html")
    for l in LEVERS + [ALL_LEVERS]:
        print(f"  levers/{l['slug']}.html")


if __name__ == "__main__":
    main()
