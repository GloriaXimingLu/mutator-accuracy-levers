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


# ===================== bottom illustration: replay-simulation ================
# A self-contained inline SVG (no CDN) illustrating the plan to A/B OpenEvolve's
# design choices (above all feature_dimensions for the MAP-Elites grid) WITHOUT
# paying for live end-to-end outer-loop rollouts: pre-compute a lineage-tree POOL
# of outer-loop variations with known (cost, accuracy), then REPLAY OpenEvolve's
# ProgramDatabase over that pool — drawing each "child" from the pool instead of
# an LLM rollout — and ask whether a cost/accuracy-frontier-best program surfaces
# as the database's best_program. Mechanics are faithful to baseline-submodules/
# openevolve/openevolve/database.py (MAP-Elites binning, one-elite-per-cell by
# fitness that excludes the feature dims, islands + ring migration, parent
# sampling: exploration / exploitation-from-archive / random).

def evolve_sim_svg() -> str:
    return r"""
<svg viewBox="0 0 1180 620" role="img"
     aria-label="Replay-simulation of OpenEvolve over a pre-computed lineage-tree pool of outer-loop runs, to cheaply ablate the MAP-Elites feature dimensions."
     xmlns="http://www.w3.org/2000/svg" class="evolve-svg">
  <!-- Self-contained palette: lives INSIDE the SVG so the figure keeps its
       colors even if the external stylesheet is missing, stale-cached, or
       blocked. Without these, every shape falls back to SVG-default
       fill:#000 and the whole illustration renders solid black. The external
       .evolve-svg rules in style.css carry identical values. -->
  <style>
    .evolve-svg .panel-box{fill:#12151c; stroke:#283040}
    .evolve-svg .svg-h{fill:#eef1f6}
    .evolve-svg .svg-sub{fill:#c2c9d4}
    .evolve-svg .svg-step{fill:#aeb6c2}
    .evolve-svg .svg-note{fill:#9aa3af}
    .evolve-svg .svg-note.dim{fill:#7d8593}
    .evolve-svg .svg-tag.accent{fill:#6ea8fe}
    .evolve-svg .svg-tag.accent2{fill:#8b9cff}
    .evolve-svg .svg-flowlab{fill:#6ea8fe}
    .evolve-svg .mono{fill:#cfe3ff}
    .evolve-svg .em{fill:#e4d9b8}
    .evolve-svg .edge{stroke:#39435a; fill:none}
    .evolve-svg .node{fill:#1d2735; stroke:#46597d}
    .evolve-svg .node.parent{fill:#243a5e; stroke:#6ea8fe}
    .evolve-svg .node.parent2{fill:#2a2f57; stroke:#8b9cff}
    .evolve-svg .node.frontier-node{fill:#1e4035; stroke:#5bd6a0}
    .evolve-svg .nlab{fill:#dfe6f0}
    .evolve-svg .frontier-tx{fill:#5bd6a0}
    .evolve-svg .flow{stroke:#6ea8fe; fill:none}
    .evolve-svg .flow.thin{stroke:#9aa3af}
    .evolve-svg .loop-bg{fill:#0f141c; stroke:#283040}
    .evolve-svg .step-card{fill:#171c26; stroke:#2e3848}
    .evolve-svg .step-card.pick{fill:#16241f; stroke:#2f5a48}
    .evolve-svg .step-tx{fill:#c8cfda}
    .evolve-svg .knob-box{fill:#161a28; stroke:#33405e}
    .evolve-svg .knob-h{fill:#8b9cff}
    .evolve-svg .knob-tx{fill:#c8cfda}
    .evolve-svg .cell{fill:#141922; stroke:#2a3342}
    .evolve-svg .cell.occ{fill:#26344a}
    .evolve-svg .cell.win{fill:#1e4035; stroke:#5bd6a0}
    .evolve-svg .winmark{fill:#5bd6a0}
    .evolve-svg .axis{stroke:#46597d; fill:none}
    .evolve-svg .axlab{fill:#9aa3af}
    .evolve-svg .verdict-box{fill:#10131a; stroke:#33405e}
    .evolve-svg .verdict-h{fill:#5bd6a0}
    .evolve-svg .verdict-tx{fill:#d5dae2}
    .evolve-svg .verdict-tx.dim{fill:#8a92a0}
  </style>
  <defs>
    <marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-end">
      <path d="M0 0L10 5L0 10z" fill="#6ea8fe"/>
    </marker>
    <marker id="arw" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-end">
      <path d="M0 0L10 5L0 10z" fill="#9aa3af"/>
    </marker>
    <linearGradient id="frontier" x1="0" y1="1" x2="1" y2="0">
      <stop offset="0" stop-color="#5bd6a0"/><stop offset="1" stop-color="#8b9cff"/>
    </linearGradient>
  </defs>

  <!-- ============================ PANEL 1 : the POOL ====================== -->
  <g>
    <rect x="14" y="40" width="372" height="540" rx="14" class="panel-box"/>
    <text x="34" y="72" class="svg-h">1 &#183; The pool we already have</text>
    <text x="34" y="95" class="svg-sub">Several real outer-loop runs (you + co-worker),</text>
    <text x="34" y="112" class="svg-sub">each seeded from a lever &#8594; a lineage tree.</text>

    <!-- lineage tree: two seeded parents, each evolving children -->
    <!-- parent A (lever-seeded) -->
    <text x="40" y="150" class="svg-tag accent">seed: lever&#160;3 (verify)</text>
    <line x1="70" y1="186" x2="150" y2="232" class="edge"/>
    <line x1="70" y1="186" x2="150" y2="300" class="edge"/>
    <line x1="150" y1="232" x2="250" y2="210" class="edge"/>
    <line x1="150" y1="232" x2="250" y2="262" class="edge"/>
    <circle cx="70"  cy="186" r="13" class="node parent"/>
    <circle cx="150" cy="232" r="11" class="node"/>
    <circle cx="150" cy="300" r="11" class="node"/>
    <circle cx="250" cy="210" r="11" class="node frontier-node"/>
    <circle cx="250" cy="262" r="11" class="node"/>
    <text x="70"  y="190" class="nlab">P</text>
    <text x="270" y="194" class="nlab small frontier-tx">&#9670; frontier-best</text>

    <!-- parent B (different lever seed) -->
    <text x="40" y="372" class="svg-tag accent2">seed: lever&#160;1 (retrieval)</text>
    <line x1="70" y1="408" x2="150" y2="408" class="edge"/>
    <line x1="150" y1="408" x2="250" y2="386" class="edge"/>
    <line x1="150" y1="408" x2="250" y2="438" class="edge"/>
    <line x1="70" y1="408" x2="150" y2="476" class="edge"/>
    <circle cx="70"  cy="408" r="13" class="node parent2"/>
    <circle cx="150" cy="408" r="11" class="node"/>
    <circle cx="250" cy="386" r="11" class="node"/>
    <circle cx="250" cy="438" r="11" class="node frontier-node"/>
    <circle cx="150" cy="476" r="11" class="node"/>
    <text x="70" y="412" class="nlab">P</text>

    <!-- each node carries (cost, acc) -->
    <text x="34" y="528" class="svg-note">each node = one committed harness variant,</text>
    <text x="34" y="546" class="svg-note">with a measured <tspan class="mono">(cost, accuracy)</tspan> already in hand.</text>
    <text x="34" y="566" class="svg-note frontier-tx">&#9670; = known best on the cost&#8211;accuracy frontier.</text>
  </g>

  <!-- arrow pool -> sim -->
  <line x1="392" y1="300" x2="436" y2="300" class="flow" marker-end="url(#ar)"/>
  <text x="414" y="290" class="svg-flowlab" text-anchor="middle">replay</text>

  <!-- ===================== PANEL 2 : the SIMULATION ====================== -->
  <g>
    <rect x="442" y="40" width="356" height="540" rx="14" class="panel-box"/>
    <text x="462" y="72" class="svg-h">2 &#183; Simulated OpenEvolve</text>
    <text x="462" y="95" class="svg-sub">Run the real <tspan class="mono">ProgramDatabase</tspan> loop &#8212;</text>
    <text x="462" y="112" class="svg-sub">but a &#8220;child&#8221; is <tspan class="em">drawn from the pool,</tspan></text>
    <text x="462" y="129" class="svg-sub">not an LLM rollout (no live outer loop).</text>

    <!-- the loop ring -->
    <g class="loopbox">
      <rect x="470" y="150" width="300" height="232" rx="12" class="loop-bg"/>
      <text x="620" y="176" class="svg-step" text-anchor="middle">the evolution step (repeated)</text>

      <rect x="492" y="192" width="256" height="34" rx="8" class="step-card"/>
      <text x="504" y="214" class="step-tx"><tspan class="mono">sample</tspan> parent + inspirations (per island)</text>

      <rect x="492" y="236" width="256" height="34" rx="8" class="step-card pick"/>
      <text x="504" y="258" class="step-tx">pick child = pool node whose parent matches</text>

      <rect x="492" y="280" width="256" height="34" rx="8" class="step-card"/>
      <text x="504" y="302" class="step-tx"><tspan class="mono">add()</tspan> &#8594; bin it, keep 1 elite / cell</text>

      <rect x="492" y="324" width="256" height="34" rx="8" class="step-card"/>
      <text x="504" y="346" class="step-tx">round-robin islands &#183; ring <tspan class="mono">migrate</tspan></text>

      <!-- recirculation arrow -->
      <path d="M748 343 q22 0 22 -28 v-96 q0 -27 -24 -27" class="flow thin" fill="none" marker-end="url(#arw)"/>
    </g>

    <text x="462" y="412" class="svg-note">Parent sampling stays faithful: exploration</text>
    <text x="462" y="430" class="svg-note">(island-random) &#183; exploitation (archive elites)</text>
    <text x="462" y="448" class="svg-note">&#183; random &#8212; weighted by fitness.</text>
    <text x="462" y="474" class="svg-note dim">Cheap: 0 LLM calls, 0 agent rollouts &#8212; just</text>
    <text x="462" y="492" class="svg-note dim">bookkeeping over points we already paid for.</text>

    <rect x="462" y="512" width="316" height="52" rx="10" class="knob-box"/>
    <text x="476" y="533" class="knob-h">ablate cheaply &#8594; the design knobs</text>
    <text x="476" y="552" class="knob-tx"><tspan class="mono">feature_dimensions</tspan> &#183; <tspan class="mono">feature_bins</tspan> &#183; #islands &#183; ratios</text>
  </g>

  <!-- arrow sim -> grid -->
  <line x1="804" y1="300" x2="848" y2="300" class="flow" marker-end="url(#ar)"/>

  <!-- ===================== PANEL 3 : grid + verdict ====================== -->
  <g>
    <rect x="854" y="40" width="312" height="540" rx="14" class="panel-box"/>
    <text x="874" y="72" class="svg-h">3 &#183; MAP-Elites grid</text>
    <text x="874" y="95" class="svg-sub">Axes = the chosen <tspan class="mono">feature_dimensions</tspan></text>
    <text x="874" y="112" class="svg-sub">(the choice under test).</text>

    <!-- grid: 5x5 cells, axes accuracy (y) vs cost (x) -->
    <g transform="translate(916,150)">
      <!-- frontier sweep behind cells -->
      <path d="M0 200 L0 120 Q90 70 200 0" stroke="url(#frontier)" stroke-width="3" fill="none" opacity="0.5"/>
      <!-- cells -->
      <g class="cellgrid">
        <rect x="0"   y="0"   width="40" height="40" class="cell"/>
        <rect x="40"  y="0"   width="40" height="40" class="cell"/>
        <rect x="80"  y="0"   width="40" height="40" class="cell occ"/>
        <rect x="120" y="0"   width="40" height="40" class="cell"/>
        <rect x="160" y="0"   width="40" height="40" class="cell win"/>
        <rect x="0"   y="40"  width="40" height="40" class="cell"/>
        <rect x="40"  y="40"  width="40" height="40" class="cell occ"/>
        <rect x="80"  y="40"  width="40" height="40" class="cell"/>
        <rect x="120" y="40"  width="40" height="40" class="cell occ"/>
        <rect x="160" y="40"  width="40" height="40" class="cell"/>
        <rect x="0"   y="80"  width="40" height="40" class="cell occ"/>
        <rect x="40"  y="80"  width="40" height="40" class="cell"/>
        <rect x="80"  y="80"  width="40" height="40" class="cell occ"/>
        <rect x="120" y="80"  width="40" height="40" class="cell"/>
        <rect x="160" y="80"  width="40" height="40" class="cell"/>
        <rect x="0"   y="120" width="40" height="40" class="cell"/>
        <rect x="40"  y="120" width="40" height="40" class="cell occ"/>
        <rect x="80"  y="120" width="40" height="40" class="cell"/>
        <rect x="120" y="120" width="40" height="40" class="cell"/>
        <rect x="160" y="120" width="40" height="40" class="cell occ"/>
        <rect x="0"   y="160" width="40" height="40" class="cell occ"/>
        <rect x="40"  y="160" width="40" height="40" class="cell"/>
        <rect x="80"  y="160" width="40" height="40" class="cell"/>
        <rect x="120" y="160" width="40" height="40" class="cell occ"/>
        <rect x="160" y="160" width="40" height="40" class="cell"/>
      </g>
      <!-- the winning cell gets the frontier marker -->
      <path d="M180 13 l7 12 l-14 0 z" class="winmark"/>
      <!-- axes -->
      <line x1="0" y1="208" x2="200" y2="208" class="axis"/>
      <line x1="-8" y1="0" x2="-8" y2="200" class="axis"/>
      <text x="100" y="230" class="axlab" text-anchor="middle">cost &#8594;</text>
      <text x="-20" y="100" class="axlab" text-anchor="middle" transform="rotate(-90,-20,100)">accuracy &#8594;</text>
    </g>

    <!-- verdict -->
    <rect x="874" y="412" width="272" height="152" rx="10" class="verdict-box"/>
    <text x="888" y="440" class="verdict-h">the question this answers</text>
    <text x="888" y="466" class="verdict-tx">Does the pool&#8217;s frontier-best</text>
    <text x="888" y="486" class="verdict-tx">program <tspan class="em">get surfaced</tspan> as the</text>
    <text x="888" y="506" class="verdict-tx">database&#8217;s <tspan class="mono">best_program</tspan>?</text>
    <text x="888" y="534" class="verdict-tx dim">If a feature choice buries it,</text>
    <text x="888" y="552" class="verdict-tx dim">that choice is wrong &#8212; reject it.</text>
  </g>
</svg>
"""


def evolve_section() -> str:
    svg = evolve_sim_svg()
    return f"""
<section class="evolve">
  <h2 class="section-title">Next: wiring the outer loop into OpenEvolve &mdash; tested by replay-simulation</h2>
  <p class="lede evolve-lede">Wiring our <strong>mutator&rsquo;s outer loop</strong> into <strong>OpenEvolve</strong>
  (the AlphaEvolve-style MAP-Elites + island evolver) forces design choices &mdash; above all the
  <strong><code>feature_dimensions</code></strong> the MAP-Elites grid bins on. Running OpenEvolve with the
  <em>live</em> outer loop inside it, end-to-end, is far too expensive to ablate. So we test the choices on a
  <strong>replay-simulation</strong> instead.</p>
  <div class="evolve-figure">
    {svg}
  </div>
  <div class="evolve-cards">
    <div class="ec">
      <span class="ec-k">The pool</span>
      <p>We already ran several outer-loop variations &mdash; each <strong>seeded from one lever</strong> and
      evolved into a <strong>lineage tree</strong> of committed harness variants. Every node has a measured
      <code>(cost, accuracy)</code>, so we know which ones sit on the <strong>cost&ndash;accuracy frontier</strong>.</p>
    </div>
    <div class="ec">
      <span class="ec-k">The trick</span>
      <p>Run the <em>real</em> OpenEvolve <code>ProgramDatabase</code> loop &mdash; sample a parent, place the
      child, keep one elite per grid cell, migrate across islands &mdash; but a &ldquo;child&rdquo; is
      <strong>drawn from the pool</strong> (the lineage child of the sampled parent) instead of a fresh LLM
      rollout. <strong>Zero rollouts, zero LLM calls.</strong></p>
    </div>
    <div class="ec">
      <span class="ec-k">The payoff</span>
      <p>Now we can A/B the OpenEvolve design knobs &mdash; <code>feature_dimensions</code>,
      <code>feature_bins</code>, island count, exploration/exploitation ratios &mdash; in seconds, and check the
      one thing that matters: <strong>does a frontier-best program get surfaced as <code>best_program</code></strong>,
      or does a bad feature choice bury it?</p>
    </div>
  </div>
  <p class="evolve-foot">Simulation mechanics mirror OpenEvolve&rsquo;s database faithfully: per-island MAP-Elites
  binning with dynamic min&ndash;max feature scaling, one elite per cell decided by a <em>fitness that excludes the
  feature dimensions</em>, ring-topology migration on a generation interval, and parent sampling split across
  exploration / archive-exploitation / random. The only substitution is the rollout &rarr; a pool lookup.</p>
</section>
"""


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
    return page("Mutator lever manuals", intro + grid + evolve_section(), depth=0)


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

/* ===== bottom illustration: OpenEvolve replay-simulation ===== */
.evolve{margin-top:60px; padding-top:34px; border-top:1px solid var(--line)}
.evolve .section-title{color:var(--accent); font-size:.9rem}
.evolve-lede{margin:0 0 22px; max-width:88ch}
.evolve-figure{
  background:linear-gradient(180deg,#10131a,#0d1016); border:1px solid var(--line);
  border-radius:16px; padding:14px; overflow-x:auto;
}
.evolve-svg{display:block; width:100%; min-width:980px; height:auto}
.evolve-cards{display:grid; grid-template-columns:repeat(3,1fr); gap:16px; margin:22px 0 8px}
.ec{background:linear-gradient(180deg,var(--panel),var(--panel2)); border:1px solid var(--line);
  border-radius:13px; padding:16px 18px}
.ec-k{display:inline-block; font-size:.66rem; letter-spacing:.14em; text-transform:uppercase;
  font-weight:700; color:var(--accent); margin-bottom:8px}
.ec p{margin:0; font-size:.92rem; line-height:1.58; color:#d0d5de}
.ec code{font-size:.84em}
.evolve-foot{margin:18px 0 8px; font-size:.86rem; line-height:1.6; color:var(--muted); max-width:92ch}

/* SVG primitives */
.evolve-svg .panel-box{fill:#12151c; stroke:#283040; stroke-width:1}
.evolve-svg .svg-h{fill:#eef1f6; font:700 16px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.evolve-svg .svg-sub{fill:#c2c9d4; font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.evolve-svg .svg-step{fill:#aeb6c2; font:600 12px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; letter-spacing:.04em}
.evolve-svg .svg-note{fill:#9aa3af; font:12.5px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.evolve-svg .svg-note.dim{fill:#7d8593}
.evolve-svg .svg-tag{font:700 12px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; letter-spacing:.02em}
.evolve-svg .svg-tag.accent{fill:#6ea8fe}
.evolve-svg .svg-tag.accent2{fill:#8b9cff}
.evolve-svg .svg-flowlab{fill:#6ea8fe; font:600 11px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.evolve-svg .mono{font-family:"SF Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; fill:#cfe3ff; font-size:.92em}
.evolve-svg .em{fill:#e4d9b8; font-style:italic}
.evolve-svg .edge{stroke:#39435a; stroke-width:1.6}
.evolve-svg .node{fill:#1d2735; stroke:#46597d; stroke-width:1.6}
.evolve-svg .node.parent{fill:#243a5e; stroke:#6ea8fe; stroke-width:2}
.evolve-svg .node.parent2{fill:#2a2f57; stroke:#8b9cff; stroke-width:2}
.evolve-svg .node.frontier-node{fill:#1e4035; stroke:#5bd6a0; stroke-width:2.4}
.evolve-svg .nlab{fill:#dfe6f0; font:600 11px -apple-system,BlinkMacSystemFont,sans-serif; text-anchor:middle}
.evolve-svg .nlab.small{font-size:11px; text-anchor:start}
.evolve-svg .frontier-tx{fill:#5bd6a0}
.evolve-svg .flow{stroke:#6ea8fe; stroke-width:2.2; fill:none}
.evolve-svg .flow.thin{stroke:#9aa3af; stroke-width:1.6}
.evolve-svg .loop-bg{fill:#0f141c; stroke:#283040; stroke-width:1}
.evolve-svg .step-card{fill:#171c26; stroke:#2e3848; stroke-width:1}
.evolve-svg .step-card.pick{fill:#16241f; stroke:#2f5a48}
.evolve-svg .step-tx{fill:#c8cfda; font:12px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.evolve-svg .knob-box{fill:#161a28; stroke:#33405e; stroke-width:1}
.evolve-svg .knob-h{fill:#8b9cff; font:700 11px -apple-system,BlinkMacSystemFont,sans-serif; letter-spacing:.08em; text-transform:uppercase}
.evolve-svg .knob-tx{fill:#c8cfda; font:12.5px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.evolve-svg .cell{fill:#141922; stroke:#2a3342; stroke-width:1}
.evolve-svg .cell.occ{fill:#26344a}
.evolve-svg .cell.win{fill:#1e4035; stroke:#5bd6a0; stroke-width:2}
.evolve-svg .winmark{fill:#5bd6a0}
.evolve-svg .axis{stroke:#46597d; stroke-width:1.4}
.evolve-svg .axlab{fill:#9aa3af; font:11px -apple-system,BlinkMacSystemFont,sans-serif}
.evolve-svg .verdict-box{fill:#10131a; stroke:#33405e; stroke-width:1}
.evolve-svg .verdict-h{fill:#5bd6a0; font:700 11px -apple-system,BlinkMacSystemFont,sans-serif; letter-spacing:.1em; text-transform:uppercase}
.evolve-svg .verdict-tx{fill:#d5dae2; font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.evolve-svg .verdict-tx.dim{fill:#8a92a0; font-size:12.5px}

@media (max-width:820px){
  .evolve-cards{grid-template-columns:1fr}
}
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
