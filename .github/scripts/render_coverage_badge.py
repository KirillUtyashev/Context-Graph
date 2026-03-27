from __future__ import annotations

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET


def _coverage_color(percent: float) -> str:
    if percent >= 90:
        return "#2ea44f"
    if percent >= 80:
        return "#3fb950"
    if percent >= 70:
        return "#9ecb3c"
    if percent >= 60:
        return "#dbab09"
    if percent >= 50:
        return "#d29922"
    return "#cf222e"


def _badge_svg(label: str, message: str, color: str) -> str:
    label_width = max(52, 7 * len(label) + 20)
    message_width = max(52, 7 * len(message) + 20)
    total_width = label_width + message_width
    message_x = label_width + message_width / 2

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total_width}" height="20" role="img" aria-label="{label}: {message}">
<linearGradient id="b" x2="0" y2="100%">
  <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
  <stop offset="1" stop-opacity=".1"/>
</linearGradient>
<mask id="a">
  <rect width="{total_width}" height="20" rx="3" fill="#fff"/>
</mask>
<g mask="url(#a)">
  <rect width="{label_width}" height="20" fill="#555"/>
  <rect x="{label_width}" width="{message_width}" height="20" fill="{color}"/>
  <rect width="{total_width}" height="20" fill="url(#b)"/>
</g>
<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
  <text x="{label_width / 2}" y="15" fill="#010101" fill-opacity=".3">{label}</text>
  <text x="{label_width / 2}" y="14">{label}</text>
  <text x="{message_x}" y="15" fill="#010101" fill-opacity=".3">{message}</text>
  <text x="{message_x}" y="14">{message}</text>
</g>
</svg>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage-xml", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    root = ET.parse(args.coverage_xml).getroot()
    line_rate = float(root.attrib.get("line-rate", "0"))
    percent = round(line_rate * 100)
    svg = _badge_svg("coverage", f"{percent}%", _coverage_color(percent))

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(svg, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
