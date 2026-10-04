"""Rescale every inline SVG so its viewBox equals its display size (1 unit = 1 px).

html_to_figma draws SVG geometry in raw viewBox units and crops it to the element box, so a
24-unit icon shown at 18 px loses its right and bottom edges. Baking the scale into the
geometry renders identically in a browser and survives the conversion.
"""
import re
import sys
from pathlib import Path

NUM = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|" + NUM.pattern)
CSS_STROKE = 1.75  # .i { stroke-width } in the stylesheet


def fmt(v):
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def scale_path(d, k):
    out, cmd, idx = [], None, 0
    for tok in TOKEN.findall(d):
        if tok.isalpha():
            cmd, idx = tok, 0
            out.append(tok)
            continue
        v = float(tok)
        if cmd in "Aa":
            pos = idx % 7  # rx ry rotation large-arc sweep x y
            out.append(tok if pos in (2, 3, 4) else fmt(v * k))
        else:
            out.append(fmt(v * k))
        idx += 1
    return " ".join(out)


def scale_attrs(tag, names, k):
    def one(m):
        return f'{m.group(1)}="{fmt(float(m.group(2)) * k)}"'
    return re.sub(r'\b(' + "|".join(names) + r')="([-\d.]+)"', one, tag)


def flatten(svg):
    head_m = re.match(r"<svg\b[^>]*>", svg)
    head = head_m.group(0)
    w = re.search(r'\swidth="([\d.]+)"', head)
    h = re.search(r'\sheight="([\d.]+)"', head)
    vb = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"', head)
    if not (w and vb):
        return svg, False
    w = float(w.group(1))
    h = float(h.group(1)) if h else w
    k = w / float(vb.group(3))
    if abs(k - 1) < 1e-6:
        return svg, False
    body = svg[len(head):]
    body = re.sub(r'\sd="([^"]*)"', lambda m: f' d="{scale_path(m.group(1), k)}"', body)
    body = re.sub(r"<(circle|rect|ellipse|line)\b[^>]*>",
                  lambda m: scale_attrs(m.group(0), ["cx", "cy", "r", "rx", "ry", "x", "y", "width", "height",
                                                     "x1", "y1", "x2", "y2", "stroke-width"], k), body)
    body = re.sub(r'(<path\b[^>]*\bstroke-width=")([\d.]+)"', lambda m: f'{m.group(1)}{fmt(float(m.group(2)) * k)}"', body)
    head = head.replace(vb.group(0), f'viewBox="0 0 {fmt(w)} {fmt(h)}"')
    # stroke width: inline style on the svg wins over the .i class, so bake it there
    is_icon = re.search(r'class="i\b', head) is not None
    style = re.search(r'style="([^"]*)"', head)
    if style and "stroke-width" in style.group(1):
        new = re.sub(r"stroke-width:\s*([\d.]+)", lambda m: f"stroke-width:{fmt(float(m.group(1)) * k)}", style.group(1))
        head = head.replace(style.group(0), f'style="{new}"')
    elif is_icon:
        if style:
            new = style.group(1).rstrip(";") + f";stroke-width:{fmt(CSS_STROKE * k)}"
            head = head.replace(style.group(0), f'style="{new}"')
        else:
            head = head[:-1] + f' style="stroke-width:{fmt(CSS_STROKE * k)}">'
    return head + body, True


def main(path):
    p = Path(path)
    html = p.read_text(encoding="utf-8")
    count = 0

    def repl(m):
        nonlocal count
        new, changed = flatten(m.group(0))
        count += changed
        return new

    html = re.sub(r"<svg\b.*?</svg>", repl, html, flags=re.S)
    p.write_text(html, encoding="utf-8", newline="")
    print(f"flattened {count} svgs")


if __name__ == "__main__":
    main(sys.argv[1])
