#!/usr/bin/env python3
"""
Generate sitemap.xml for usephalanx.com with today's date for content pages.

Content pages (/, /changelog.html, /documentation.html) get today's date —
these change on every deploy. Legal pages keep their fixed dates since they
rarely change.

Usage:
    python scripts/generate_sitemap.py [--output PATH]

Default output: /home/ubuntu/phalanx/site/sitemap.xml (production path).
Pass --output to override (e.g. for local testing).
"""

import argparse
from datetime import date

LEGAL_DATE = "2026-04-06"

URLS = [
    {"loc": "https://usephalanx.com/",                   "dynamic": True,  "changefreq": "weekly",  "priority": "1.0"},
    {"loc": "https://usephalanx.com/changelog.html",      "dynamic": True,  "changefreq": "weekly",  "priority": "0.9"},
    {"loc": "https://usephalanx.com/documentation.html",  "dynamic": True,  "changefreq": "monthly", "priority": "0.9"},
    {"loc": "https://usephalanx.com/privacy.html",        "dynamic": False, "changefreq": "yearly",  "priority": "0.3"},
    {"loc": "https://usephalanx.com/terms.html",          "dynamic": False, "changefreq": "yearly",  "priority": "0.3"},
]


def build_sitemap() -> str:
    today = date.today().isoformat()
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for url in URLS:
        lastmod = today if url["dynamic"] else LEGAL_DATE
        lines.append("  <url>")
        lines.append(f'    <loc>{url["loc"]}</loc>')
        lines.append(f'    <lastmod>{lastmod}</lastmod>')
        lines.append(f'    <changefreq>{url["changefreq"]}</changefreq>')
        lines.append(f'    <priority>{url["priority"]}</priority>')
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate sitemap.xml")
    parser.add_argument(
        "--output",
        default="/home/ubuntu/phalanx/site/sitemap.xml",
        help="Output path for sitemap.xml",
    )
    args = parser.parse_args()

    content = build_sitemap()

    with open(args.output, "w") as f:
        f.write(content)

    print(f"  ✓ sitemap.xml written to {args.output} (lastmod: {date.today().isoformat()})")


if __name__ == "__main__":
    main()
