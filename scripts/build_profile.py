#!/usr/bin/env python3
"""Build the profile README and its animated isometric SVG card.

Reads profile.toml for the hand-written content, pulls live numbers from the
GitHub GraphQL API, and writes:

    assets/profile-{dark,light}.svg   intro + stats + isometric contribution
                                       skyline (bars rise on load) + languages
    README.md                          embeds the card + plain markdown fallback

Standard library only (Python 3.11+). Needs GITHUB_TOKEN in the environment.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import math
import os
import re
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
API_URL = "https://api.github.com/graphql"

SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"

# `ramp` colours the skyline by GitHub's contribution level (none → 4th quartile).
PALETTES = {
    "dark": {
        "bg": "#0b0e14", "glow": "#16304a", "border": "#262c36",
        "text": "#e6edf3", "muted": "#8b949e", "accent": "#67e8f9", "accent2": "#c4b5fd",
        "ramp": ["#1c232e", "#155e75", "#0e7490", "#06b6d4", "#a5f3fc"],
    },
    "light": {
        "bg": "#ffffff", "glow": "#dff3f8", "border": "#d0d7de",
        "text": "#1f2328", "muted": "#59636e", "accent": "#0e7490", "accent2": "#6d28d9",
        "ramp": ["#e3e8ee", "#a5f3fc", "#22d3ee", "#0891b2", "#155e75"],
    },
}

HEAT_LEVEL = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2, "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}


# --------------------------------------------------------------------------- data

def graphql(query: str, variables: dict, token: str) -> dict:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-readme-builder",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise RuntimeError(json.dumps(body["errors"], indent=2))
    return body["data"]


USER_QUERY = """
query($login: String!) {
  user(login: $login) {
    followers { totalCount }
    pullRequests { totalCount }
    issues { totalCount }
    repositoriesContributedTo(contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]) { totalCount }
    contributionsCollection {
      totalCommitContributions
      contributionYears
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date weekday contributionCount contributionLevel } }
      }
    }
  }
}
"""

REPOS_QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    repositories(ownerAffiliations: OWNER, privacy: PUBLIC, isFork: false, first: 100, after: $after) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        stargazerCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""


def fetch_stats(cfg: dict, token: str) -> dict:
    login = cfg["username"]
    user = graphql(USER_QUERY, {"login": login}, token)["user"]
    recent = user["contributionsCollection"]

    # Repos: stars + language bytes, paginated.
    repos, after = [], None
    while True:
        page = graphql(REPOS_QUERY, {"login": login, "after": after}, token)["user"]["repositories"]
        repos += page["nodes"]
        repo_count = page["totalCount"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]

    # Every contribution year, for all-time totals and streaks. A collection
    # may span at most one year, so alias one per year in a single query.
    now = dt.datetime.now(dt.timezone.utc)
    parts = []
    for year in recent["contributionYears"]:
        end = min(dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc), now)
        parts.append(
            f'y{year}: contributionsCollection(from: "{year}-01-01T00:00:00Z", to: "{end:%Y-%m-%dT%H:%M:%SZ}") '
            "{ contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } } }"
        )
    years = graphql(f'query($login: String!) {{ user(login: $login) {{ {" ".join(parts)} }} }}', {"login": login}, token)["user"]

    daily: dict[str, int] = {}
    all_time = 0
    for coll in years.values():
        cal = coll["contributionCalendar"]
        all_time += cal["totalContributions"]
        for week in cal["weeks"]:
            for day in week["contributionDays"]:
                daily[day["date"]] = day["contributionCount"]

    current, longest = streaks(daily, now.date())
    return {
        "stars": sum(r["stargazerCount"] for r in repos),
        "repos": repo_count,
        "followers": user["followers"]["totalCount"],
        "prs": user["pullRequests"]["totalCount"],
        "issues": user["issues"]["totalCount"],
        "contributed_to": user["repositoriesContributedTo"]["totalCount"],
        "commits_year": recent["totalCommitContributions"],
        "contrib_year": recent["contributionCalendar"]["totalContributions"],
        "contrib_all": all_time,
        "since": min(recent["contributionYears"]),
        "streak_current": current,
        "streak_longest": longest,
        "languages": top_languages(repos, cfg.get("stats", {})),
        "calendar": recent["contributionCalendar"]["weeks"],
        "updated": now.date().isoformat(),
    }


def streaks(daily: dict[str, int], today: dt.date) -> tuple[int, int]:
    longest = run = 0
    for date in sorted(daily):
        if date > today.isoformat():
            break
        run = run + 1 if daily[date] else 0
        longest = max(longest, run)
    # Today not having a contribution *yet* shouldn't break the streak.
    day = today if daily.get(today.isoformat()) else today - dt.timedelta(days=1)
    current = 0
    while daily.get(day.isoformat()):
        current += 1
        day -= dt.timedelta(days=1)
    return current, longest


def top_languages(repos: list[dict], opts: dict) -> list[dict]:
    exclude = set(opts.get("exclude_languages", []))
    langs: dict[str, dict] = {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            if name in exclude:
                continue
            entry = langs.setdefault(name, {"name": name, "color": edge["node"]["color"] or "#8b949e", "bytes": 0, "repos": 0})
            entry["bytes"] += edge["size"]
            entry["repos"] += 1
    # Blend byte volume with how many repos use the language, so one giant
    # vendored repo doesn't flatten everything else.
    for entry in langs.values():
        entry["score"] = math.sqrt(entry["bytes"]) * math.sqrt(entry["repos"])
    top = sorted(langs.values(), key=lambda e: e["score"], reverse=True)[: opts.get("top_languages", 8)]
    total = sum(e["score"] for e in top) or 1
    for entry in top:
        entry["pct"] = 100 * entry["score"] / total
    return top




# --------------------------------------------------------------------------- helpers

INLINE = re.compile(r"\[(\*\*)?(.+?)(?:\*\*)?\]\((.+?)\)|\*\*(.+?)\*\*")


def inline_segments(text: str) -> list[tuple[str, str]]:
    """Split our tiny markdown subset into (kind, text) runs: plain/bold/link."""
    out, pos = [], 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            out.append(("plain", text[pos:m.start()]))
        out.append(("link", m.group(2)) if m.group(3) else ("bold", m.group(4)))
        pos = m.end()
    if pos < len(text):
        out.append(("plain", text[pos:]))
    return out


def plain(text: str) -> str:
    return "".join(t for _, t in inline_segments(text))


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def fmt(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 100_000 else f"{n:,}"


def shade(color: str, k: float) -> str:
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#{:02x}{:02x}{:02x}".format(*(min(255, round(c * k)) for c in (r, g, b)))


def pts(points) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


# --------------------------------------------------------------------------- card

def render_card(cfg: dict, s: dict, p: dict) -> str:
    W, pad = 850, 34
    front: list[str] = []  # text layers, drawn over the skyline

    # Intro, top-left.
    front.append(
        f'<text class="a over" x="{pad}" y="44" fill="{p["muted"]}" style="animation-delay:100ms">'
        f'@{esc(cfg["username"])}  ·  {esc(cfg["handle"])}</text>'
        f'<text class="a name" x="{pad - 1}" y="80" fill="{p["text"]}" style="animation-delay:200ms">'
        f'{esc(cfg["name"].upper())}</text>'
        f'<text class="a tag" x="{pad}" y="106" fill="{p["accent"]}" style="animation-delay:320ms">'
        f'{esc(cfg["tagline"])}</text>'
    )
    style = {"plain": "", "bold": f' fill="{p["text"]}" font-weight="600"', "link": f' fill="{p["accent2"]}" font-weight="600"'}
    y = 138
    for i, line in enumerate(cfg["about"]):
        runs = "".join(f"<tspan{style[k]}>{esc(t)}</tspan>" for k, t in inline_segments(line))
        front.append(
            f'<g class="a" style="animation-delay:{440 + i * 90}ms">'
            f'<path d="M{pad} {y - 4} l4 -2.5 l4 2.5 l-4 2.5 Z" fill="{p["accent"]}"/>'
            f'<text class="about" x="{pad + 16}" y="{y}" fill="{p["muted"]}">{runs}</text></g>'
        )
        y += 22
    intro_bottom = y - 10

    # Stats, top-right: 2 columns x 4 rows.
    stats = [
        ("Stars", s["stars"]), ("Followers", s["followers"]),
        ("Public repos", s["repos"]), ("Contributed to", s["contributed_to"]),
        ("Pull requests", s["prs"]), ("Issues", s["issues"]),
        ("Commits · 12 mo", s["commits_year"]), (f"Contribs · since {s['since']}", s["contrib_all"]),
    ]
    sx, sy, cw, rh = 612, 40, 104, 46
    for i, (label, value) in enumerate(stats):
        x, y0 = sx + (i % 2) * cw, sy + (i // 2) * rh
        front.append(
            f'<g class="a" style="animation-delay:{300 + i * 70}ms">'
            f'<text class="num" x="{x}" y="{y0 + 18}" fill="{p["text"]}">{fmt(value)}</text>'
            f'<text class="lbl" x="{x}" y="{y0 + 34}" fill="{p["muted"]}">{esc(label)}</text></g>'
        )
    stats_bottom = sy + 4 * rh - 8

    # Caption, left, between intro and the low end of the skyline.
    cap_y = intro_bottom + 40
    front.append(
        f'<g class="a" style="animation-delay:900ms">'
        f'<text class="big" x="{pad}" y="{cap_y}" fill="{p["text"]}">{fmt(s["contrib_year"])}'
        f'<tspan class="lbl" fill="{p["muted"]}" dx="8">contributions in the last year</tspan></text>'
        f'<text class="lbl" x="{pad}" y="{cap_y + 20}" fill="{p["muted"]}">streak '
        f'<tspan fill="{p["text"]}" font-weight="600">{s["streak_current"]} d</tspan>  ·  best '
        f'<tspan fill="{p["text"]}" font-weight="600">{s["streak_longest"]} d</tspan></text></g>'
    )
    keep_clear = [(pad, 590, intro_bottom), (pad, pad + 330, cap_y + 26), (590, W, stats_bottom)]

    # Isometric skyline. Weeks run along u (rising to the right), weekdays
    # along v (towards the viewer); bar height is up the screen.
    weeks = s["calendar"]
    n = len(weeks)
    vx, vy = 7.5, 6.0
    ux = (W - 2 * pad - 7 * vx) / n
    uy = ux * 0.15
    hmax, inset, plate = 92, 0.12, 7
    peak = max((d["contributionCount"] for wk in weeks for d in wk["contributionDays"]), default=0) or 1

    def bar_h(count: int) -> float:
        return 0 if not count else 4 + (hmax - 4) * math.sqrt(count / peak)

    cells = [(wi, day["weekday"], day["contributionCount"], HEAT_LEVEL.get(day["contributionLevel"], 0))
             for wi, wk in enumerate(weeks) for day in wk["contributionDays"]]

    # Drop the grid as high as the text above it allows.
    oy = 0.0
    for w, d, count, _ in cells:
        x0, x1 = pad + w * ux + d * vx, pad + (w + 1) * ux + (d + 1) * vx
        for bx0, bx1, bottom in keep_clear:
            if x1 > bx0 and x0 < bx1:
                oy = max(oy, bottom + 14 + bar_h(count) + uy * (w + 1) - vy * d)

    def B(w: float, d: float, h: float = 0.0) -> tuple[float, float]:
        return pad + w * ux + d * vx, oy - w * uy + d * vy - h

    ramp = p["ramp"]
    sky: list[str] = []

    # Base plate the bars stand on.
    g0, g1 = -0.4, 0.4
    top = [B(g0, g0), B(n + g1, g0), B(n + g1, 7 + g1), B(g0, 7 + g1)]
    sky.append(
        f'<polygon points="{pts(top)}" fill="{shade(ramp[0], 0.82)}"/>'
        f'<polygon points="{pts([B(g0, g0), B(g0, 7 + g1), B(g0, 7 + g1, -plate), B(g0, g0, -plate)])}" fill="{shade(ramp[0], 0.74)}"/>'
        f'<polygon points="{pts([B(g0, 7 + g1), B(n + g1, 7 + g1), B(n + g1, 7 + g1, -plate), B(g0, 7 + g1, -plate)])}" fill="{shade(ramp[0], 0.64)}"/>'
    )
    tiles = "".join(
        f'<polygon points="{pts([B(w + inset, d + inset), B(w + 1 - inset, d + inset), B(w + 1 - inset, d + 1 - inset), B(w + inset, d + 1 - inset)])}"/>'
        for w, d, _, _ in cells
    )
    sky.append(f'<g fill="{ramp[0]}">{tiles}</g>')

    # Bars, painted back to front. Each face animates from flat to full height.
    grow = 650
    for w, d, count, level in sorted(cells, key=lambda c: (c[1], -c[0])):
        h = bar_h(count)
        if not h:
            continue
        a, b = w + inset, w + 1 - inset
        c, e = d + inset, d + 1 - inset
        color = ramp[max(level, 1)]
        delay = 700 + w * 26 + d * 22
        faces = [
            ([B(a, c), B(b, c), B(b, e), B(a, e)], [B(a, c, h), B(b, c, h), B(b, e, h), B(a, e, h)], color),
            ([B(a, c), B(a, e), B(a, e), B(a, c)], [B(a, c), B(a, e), B(a, e, h), B(a, c, h)], shade(color, 0.72)),
            ([B(a, e), B(b, e), B(b, e), B(a, e)], [B(a, e), B(b, e), B(b, e, h), B(a, e, h)], shade(color, 0.55)),
        ]
        for flat, full, fill in faces:
            sky.append(
                f'<polygon points="{pts(full)}" fill="{fill}"><animate attributeName="points" '
                f'values="{pts(flat)};{pts(flat)};{pts(full)}" keyTimes="0;{delay / (delay + grow):.3f};1" '
                f'dur="{(delay + grow) / 1000:.2f}s" calcMode="spline" keySplines="0 0 1 1;.2 .8 .3 1" fill="freeze"/></polygon>'
            )

    # Date range under the front edge.
    first = dt.date.fromisoformat(weeks[0]["contributionDays"][0]["date"])
    last = dt.date.fromisoformat(weeks[-1]["contributionDays"][-1]["date"])
    lx, ly = B(g0, 7 + g1, -plate)
    rx, ry = B(n + g1, 7 + g1, -plate)
    front.append(
        f'<text class="a tiny" x="{lx:.1f}" y="{ly + 16:.1f}" fill="{p["muted"]}" style="animation-delay:1200ms">{first:%b %Y}</text>'
        f'<text class="a tiny" x="{rx:.1f}" y="{ry + 16:.1f}" text-anchor="end" fill="{p["muted"]}" style="animation-delay:2400ms">{last:%b %Y}</text>'
    )

    # Top languages, bottom-right, tucked under the skyline's front edge.
    langs = s["languages"]
    lx0 = 404
    lw = W - pad - lx0
    edge_y = B(0, 7 + g1, -plate)[1] - (lx0 - B(0, 7 + g1)[0]) * uy / ux
    ly0 = max(edge_y + 34, ry + 52)
    front.append(f'<text class="a h2" x="{lx0}" y="{ly0:.1f}" fill="{p["text"]}" style="animation-delay:1600ms">Top languages</text>')
    by = ly0 + 12
    segs, bx = [], float(lx0)
    for i, lang in enumerate(langs):
        sw = lw * lang["pct"] / 100
        segs.append(f'<rect class="seg" x="{bx:.2f}" y="{by:.1f}" width="{sw + .5:.2f}" height="8" fill="{lang["color"]}" '
                    f'style="animation-delay:{1700 + i * 100}ms"/>')
        bx += sw
    front.append(
        f'<clipPath id="bar"><rect x="{lx0}" y="{by:.1f}" width="{lw}" height="8" rx="4"/></clipPath>'
        f'<rect x="{lx0}" y="{by:.1f}" width="{lw}" height="8" rx="4" fill="{ramp[0]}"/>'
        f'<g clip-path="url(#bar)">{"".join(segs)}</g>'
    )
    cols = 3
    for i, lang in enumerate(langs):
        x = lx0 + (i % cols) * (lw / cols)
        y0 = by + 30 + (i // cols) * 20
        front.append(
            f'<g class="a" style="animation-delay:{1800 + i * 70}ms">'
            f'<circle cx="{x + 4:.1f}" cy="{y0 - 4:.1f}" r="4" fill="{lang["color"]}"/>'
            f'<text class="lbl" x="{x + 13:.1f}" y="{y0:.1f}" fill="{p["text"]}">{esc(lang["name"])}'
            f'<tspan fill="{p["muted"]}"> {lang["pct"]:.1f}%</tspan></text></g>'
        )
    H = round(max(by + 30 + (math.ceil(len(langs) / cols) - 1) * 20, ly + 16) + pad - 6)

    cx, cy = B(n / 2, 3.5)
    css = (
        f"text {{ font-family: {SANS}; }}\n"
        ".over { font-size: 12px; letter-spacing: .08em; } "
        ".name { font-size: 30px; font-weight: 800; letter-spacing: .04em; } "
        ".tag { font-size: 14px; font-weight: 600; letter-spacing: .02em; }\n"
        ".about { font-size: 12.5px; } .num { font-size: 20px; font-weight: 700; } "
        ".big { font-size: 22px; font-weight: 700; } .lbl { font-size: 11.5px; font-weight: 400; } "
        ".h2 { font-size: 12.5px; font-weight: 600; } .tiny { font-size: 11px; }\n"
        ".a { opacity: 0; animation: fadeup .5s ease-out forwards; }\n"
        ".seg { transform-box: fill-box; transform-origin: left center; transform: scaleX(0); "
        "animation: grow .7s cubic-bezier(.2,.8,.2,1) forwards; }\n"
        "@keyframes fadeup { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }\n"
        "@keyframes grow { to { transform: scaleX(1); } }\n"
        "@media (prefers-reduced-motion: reduce) { .a, .seg { animation: none; opacity: 1; transform: none; } }"
    )
    title = f"{cfg['name']} — {cfg['tagline']}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" xml:space="preserve" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" role="img" aria-label="{esc(card_alt(cfg, s))}">\n'
        f"<title>{esc(title)}</title>\n<style>{css}</style>\n"
        f'<defs><radialGradient id="glow"><stop offset="0" stop-color="{p["glow"]}"/>'
        f'<stop offset="1" stop-color="{p["glow"]}" stop-opacity="0"/></radialGradient></defs>\n'
        f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="14" fill="{p["bg"]}" stroke="{p["border"]}"/>\n'
        f'<ellipse cx="{cx:.0f}" cy="{cy:.0f}" rx="{W * 0.45:.0f}" ry="150" fill="url(#glow)"/>\n'
        + "\n".join(sky) + "\n" + "\n".join(front) + "\n</svg>\n"
    )


def card_alt(cfg: dict, s: dict) -> str:
    about = " ".join(plain(line) + "." for line in cfg["about"])
    langs = ", ".join(lang["name"] for lang in s["languages"][:5])
    return (
        f"{cfg['name']} (@{cfg['handle']}) — {cfg['tagline']}. {about} "
        f"GitHub: {fmt(s['contrib_year'])} contributions in the last year ({fmt(s['contrib_all'])} since {s['since']}), "
        f"{fmt(s['stars'])} stars, {fmt(s['prs'])} pull requests, current streak {s['streak_current']} days, "
        f"longest {s['streak_longest']} days. Top languages: {langs}."
    )


# --------------------------------------------------------------------------- README

def render_readme(cfg: dict, s: dict) -> str:
    about = "\n".join(f"- {line}" for line in cfg["about"])
    links = "\n".join(f"- {link['label']}: [{link['text']}]({link['url']})" for link in cfg.get("links", []))
    return f"""<!--
  Generated by scripts/build_profile.py from profile.toml — edit those, not this file.
  Refreshed weekly by .github/workflows/update-profile.yml.
-->

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/profile-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="assets/profile-light.svg">
  <img alt="{esc(card_alt(cfg, s))}" src="assets/profile-dark.svg" width="100%">
</picture>

<details>
<summary><b>About Me</b> (plain text)</summary>

{about}

</details>

## Links
{links}

<sub>Stats last refreshed {s["updated"]} · rebuilt weekly by GitHub Actions</sub>
"""


# --------------------------------------------------------------------------- main

def main() -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("GITHUB_TOKEN is not set (locally: GITHUB_TOKEN=$(gh auth token) python3 scripts/build_profile.py)", file=sys.stderr)
        return 1
    cfg = tomllib.loads((ROOT / "profile.toml").read_text(encoding="utf-8"))
    stats = fetch_stats(cfg, token)

    ASSETS.mkdir(exist_ok=True)
    for theme, palette in PALETTES.items():
        (ASSETS / f"profile-{theme}.svg").write_text(render_card(cfg, stats, palette), encoding="utf-8")
    (ROOT / "README.md").write_text(render_readme(cfg, stats), encoding="utf-8")

    summary = {k: v for k, v in stats.items() if k not in ("calendar", "languages")}
    summary["languages"] = [f"{lang['name']} {lang['pct']:.1f}%" for lang in stats["languages"]]
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
