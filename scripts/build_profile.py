#!/usr/bin/env python3
"""Build the profile README and its animated SVGs.

Reads profile.toml for the hand-written content, pulls live numbers from the
GitHub GraphQL API, and writes:

    assets/terminal-{dark,light}.svg   animated "whoami" terminal card
    assets/stats-{dark,light}.svg      animated stats / languages / heatmap card
    README.md                          embeds the SVGs + plain markdown fallback

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

MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', 'DejaVu Sans Mono', monospace"
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"

PALETTES = {
    "dark": {
        "bg": "#0d1117", "panel": "#161b22", "border": "#30363d",
        "text": "#e6edf3", "muted": "#8b949e", "accent": "#58a6ff",
        "green": "#3fb950", "purple": "#d2a8ff", "orange": "#f0883e", "pink": "#ff7b72",
        "heat": ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"],
    },
    "light": {
        "bg": "#ffffff", "panel": "#f6f8fa", "border": "#d0d7de",
        "text": "#1f2328", "muted": "#59636e", "accent": "#0969da",
        "green": "#1a7f37", "purple": "#8250df", "orange": "#bc4c00", "pink": "#cf222e",
        "heat": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
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


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def fmt(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 100_000 else f"{n:,}"


def svg_doc(width: int, height: int, title: str, css: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" xml:space="preserve" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">\n'
        f"<title>{esc(title)}</title>\n<style>{css}\n"
        "@media (prefers-reduced-motion: reduce) { * { animation: none !important; } "
        ".a, .tile, .wk { opacity: 1 !important; } .ch { fill-opacity: 1 !important; } "
        ".seg { transform: none !important; } }\n"
        f"</style>\n{body}\n</svg>\n"
    )


# --------------------------------------------------------------------------- terminal card

def render_terminal(cfg: dict, p: dict) -> str:
    W, pad, top, lh, fs = 850, 28, 64, 24, 14
    char_w = fs * 0.6
    type_ms, line_ms, pause_ms = 42, 90, 280
    prompt = f"{cfg['handle']}@github:~$"
    cmd_x = pad + (len(prompt) + 1) * char_w

    rows: list[str] = []
    t = 400  # ms since load
    y = top

    def command(cmd: str) -> None:
        nonlocal t, y
        chars = "".join(
            f'<tspan class="ch" style="animation-delay:{t + i * type_ms}ms">{esc(c)}</tspan>'
            for i, c in enumerate(cmd)
        )
        rows.append(
            f'<text class="a" x="{pad}" y="{y}" style="animation-delay:{t - 150}ms">'
            f'<tspan fill="{p["green"]}">{esc(prompt)}</tspan></text>'
            f'<text x="{cmd_x:.1f}" y="{y}" fill="{p["text"]}">{chars}</text>'
        )
        t += len(cmd) * type_ms + pause_ms
        y += lh

    def output(inner: str, x: float = pad + 16) -> None:
        nonlocal t, y
        rows.append(f'<text class="a" x="{x:.1f}" y="{y}" style="animation-delay:{t}ms">{inner}</text>')
        t += line_ms
        y += lh

    def runs(text: str) -> str:
        style = {"plain": f'fill="{p["text"]}"', "bold": f'fill="{p["accent"]}" font-weight="700"',
                 "link": f'fill="{p["purple"]}" font-weight="700" text-decoration="underline"'}
        return "".join(f"<tspan {style[k]}>{esc(s)}</tspan>" for k, s in inline_segments(text))

    command("whoami")
    output(f'<tspan fill="{p["text"]}">{esc(cfg["greeting"])}  I\'m </tspan>'
           f'<tspan fill="{p["accent"]}" font-weight="700">{esc(cfg["name"])}</tspan>'
           f'<tspan fill="{p["muted"]}"> (@{esc(cfg["handle"])})</tspan>')
    t += pause_ms
    y += 8

    command("cat about.md")
    for line in cfg["about"]:
        output(f'<tspan fill="{p["orange"]}">• </tspan>{runs(line)}')
    t += pause_ms
    y += 8

    command("ls ~/links")
    links = cfg.get("links", [])
    half = math.ceil(len(links) / 2)
    label_w = max((len(link["label"]) for link in links), default=0) + 2
    for i in range(half):
        cells = []
        for col, link in enumerate(links[i::half][:2]):
            x = pad + 16 + col * 400
            cells.append(
                f'<tspan x="{x}" fill="{p["accent"]}">{esc(link["label"].lower().ljust(label_w))}</tspan>'
                f'<tspan fill="{p["muted"]}">{esc(link["text"])}</tspan>'
            )
        output("".join(cells))
    t += pause_ms
    y += 8

    # Idle prompt with a blinking block cursor.
    rows.append(
        f'<text class="a" x="{pad}" y="{y}" style="animation-delay:{t}ms" fill="{p["green"]}">{esc(prompt)}</text>'
        f'<rect class="cursor" x="{cmd_x:.1f}" y="{y - fs + 1}" width="{char_w:.1f}" height="{fs + 3}" '
        f'fill="{p["text"]}" style="animation-delay:{t}ms"/>'
    )

    H = y + pad
    css = (
        f"text {{ font: {fs}px {MONO}; white-space: pre; }}\n"
        ".title { font-size: 12px; }\n"
        ".a { opacity: 0; animation: fadeup .4s ease-out forwards; }\n"
        ".ch { fill-opacity: 0; animation: type 10ms linear forwards; }\n"
        ".cursor { opacity: 0; animation: blink 1.1s step-end infinite; }\n"
        "@keyframes fadeup { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: none; } }\n"
        "@keyframes type { to { fill-opacity: 1; } }\n"
        "@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }"
    )
    body = (
        f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="12" fill="{p["bg"]}" stroke="{p["border"]}"/>\n'
        f'<path d="M.5 36.5 V12.5 a12 12 0 0 1 12 -12 H{W - 12.5} a12 12 0 0 1 12 12 V36.5 Z" fill="{p["panel"]}"/>\n'
        f'<line x1=".5" y1="36.5" x2="{W - .5}" y2="36.5" stroke="{p["border"]}"/>\n'
        '<circle cx="22" cy="18.5" r="6" fill="#ff5f57"/><circle cx="42" cy="18.5" r="6" fill="#febc2e"/>'
        '<circle cx="62" cy="18.5" r="6" fill="#28c840"/>\n'
        f'<text class="title" x="{W / 2}" y="23" text-anchor="middle" fill="{p["muted"]}">'
        f'{esc(cfg["handle"])}@github: ~ — zsh</text>\n'
        + "\n".join(rows)
    )
    return svg_doc(W, H, f"{cfg['greeting']} I'm {cfg['name']}", css, body)


# --------------------------------------------------------------------------- stats card

def render_stats(cfg: dict, s: dict, p: dict) -> str:
    W, pad = 850, 30
    inner = W - 2 * pad
    parts: list[str] = []

    parts.append(
        f'<text x="{pad}" y="40" class="h1" fill="{p["text"]}">GitHub Stats</text>'
        f'<text x="{W - pad}" y="40" class="small" text-anchor="end" fill="{p["muted"]}">'
        f'@{esc(cfg["username"])} · updated {s["updated"]}</text>'
    )

    # Tiles: 5 x 2.
    tiles = [
        ("Stars earned", fmt(s["stars"]), p["orange"]),
        ("Total contributions", fmt(s["contrib_all"]), p["green"]),
        ("Commits (past year)", fmt(s["commits_year"]), p["accent"]),
        ("Pull requests", fmt(s["prs"]), p["purple"]),
        ("Issues", fmt(s["issues"]), p["pink"]),
        ("Contributed to", fmt(s["contributed_to"]), p["accent"]),
        ("Current streak", f'{fmt(s["streak_current"])} days', p["orange"]),
        ("Longest streak", f'{fmt(s["streak_longest"])} days', p["green"]),
        ("Public repos", fmt(s["repos"]), p["purple"]),
        ("Followers", fmt(s["followers"]), p["pink"]),
    ]
    cols, gap, th, ty = 5, 12, 66, 62
    tw = (inner - (cols - 1) * gap) / cols
    for i, (label, value, color) in enumerate(tiles):
        x = pad + (i % cols) * (tw + gap)
        y = ty + (i // cols) * (th + gap)
        parts.append(
            f'<g class="tile" style="animation-delay:{150 + i * 70}ms">'
            f'<rect x="{x:.1f}" y="{y}" width="{tw:.1f}" height="{th}" rx="8" fill="{p["panel"]}" stroke="{p["border"]}"/>'
            f'<rect x="{x:.1f}" y="{y + 14}" width="3" height="{th - 28}" rx="1.5" fill="{color}"/>'
            f'<text x="{x + 16:.1f}" y="{y + 25}" class="label" fill="{p["muted"]}">{esc(label)}</text>'
            f'<text x="{x + 16:.1f}" y="{y + 51}" class="value" fill="{p["text"]}">{esc(value)}</text></g>'
        )
    y = ty + 2 * th + gap + 38

    # Top languages: stacked bar + legend.
    langs = s["languages"]
    parts.append(f'<text x="{pad}" y="{y}" class="h2" fill="{p["text"]}">Top languages</text>')
    y += 14
    parts.append(f'<clipPath id="bar"><rect x="{pad}" y="{y}" width="{inner}" height="10" rx="5"/></clipPath>'
                 f'<rect x="{pad}" y="{y}" width="{inner}" height="10" rx="5" fill="{p["panel"]}"/>')
    bx, segs = float(pad), []
    for i, lang in enumerate(langs):
        w = inner * lang["pct"] / 100
        segs.append(f'<rect class="seg" x="{bx:.2f}" y="{y}" width="{w + .5:.2f}" height="10" fill="{lang["color"]}" '
                    f'style="animation-delay:{900 + i * 110}ms"/>')
        bx += w
    parts.append(f'<g clip-path="url(#bar)">{"".join(segs)}</g>')
    y += 34
    lcols = 4
    for i, lang in enumerate(langs):
        x = pad + (i % lcols) * (inner / lcols)
        ly = y + (i // lcols) * 24
        parts.append(
            f'<g class="tile" style="animation-delay:{1000 + i * 70}ms">'
            f'<circle cx="{x + 5:.1f}" cy="{ly - 4}" r="5" fill="{lang["color"]}"/>'
            f'<text x="{x + 16:.1f}" y="{ly}" class="label" fill="{p["text"]}">{esc(lang["name"])}'
            f'<tspan fill="{p["muted"]}"> {lang["pct"]:.1f}%</tspan></text></g>'
        )
    y += math.ceil(len(langs) / lcols) * 24 + 30

    # Contribution heatmap (rolling 12 months, same as github.com).
    weeks = s["calendar"]
    parts.append(f'<text x="{pad}" y="{y}" class="h2" fill="{p["text"]}">'
                 f'{fmt(s["contrib_year"])} contributions in the last year</text>')
    y += 26
    pitch = inner / len(weeks)
    cell = pitch - 3
    starts = [wi for wi, week in enumerate(weeks)
              if not wi or week["contributionDays"][0]["date"][5:7] != weeks[wi - 1]["contributionDays"][0]["date"][5:7]]
    for i, wi in enumerate(starts):
        if i + 1 < len(starts) and starts[i + 1] - wi < 3:
            continue  # partial month squeezed against the next label
        month = dt.date.fromisoformat(weeks[wi]["contributionDays"][0]["date"]).strftime("%b")
        parts.append(f'<text x="{pad + wi * pitch:.1f}" y="{y}" class="tiny" fill="{p["muted"]}">{month}</text>')
    y += 8
    today = s["updated"]
    heat = p["heat"]
    for wi, week in enumerate(weeks):
        cells = []
        for day in week["contributionDays"]:
            lvl = HEAT_LEVEL.get(day["contributionLevel"], 0)
            cls = ' class="today"' if day["date"] == today else ""
            cells.append(
                f'<rect{cls} x="{pad + wi * pitch:.1f}" y="{y + day["weekday"] * pitch:.1f}" width="{cell:.1f}" '
                f'height="{cell:.1f}" rx="2.5" fill="{heat[lvl]}">'
                f'<title>{day["contributionCount"]} on {day["date"]}</title></rect>'
            )
        parts.append(f'<g class="wk" style="animation-delay:{1500 + wi * 22}ms">{"".join(cells)}</g>')
    y += 7 * pitch + 18

    # Legend.
    lx = W - pad - 5 * 15 - 34
    parts.append(f'<text x="{lx - 8}" y="{y + 9}" class="tiny" text-anchor="end" fill="{p["muted"]}">Less</text>')
    for i, color in enumerate(heat):
        parts.append(f'<rect x="{lx + i * 15}" y="{y}" width="11" height="11" rx="2.5" fill="{color}"/>')
    parts.append(f'<text x="{lx + 5 * 15 + 4}" y="{y + 9}" class="tiny" fill="{p["muted"]}">More</text>')
    H = int(y + 11 + pad)

    css = (
        f"text {{ font-family: {SANS}; }}\n"
        ".h1 { font-size: 20px; font-weight: 700; } .h2 { font-size: 14px; font-weight: 600; }\n"
        ".label { font-size: 12.5px; } .value { font-size: 22px; font-weight: 700; } "
        ".small { font-size: 12.5px; } .tiny { font-size: 11px; }\n"
        ".tile, .wk { opacity: 0; animation: fadeup .5s ease-out forwards; }\n"
        ".seg { transform-box: fill-box; transform-origin: left center; transform: scaleX(0); "
        "animation: grow .7s cubic-bezier(.2,.8,.2,1) forwards; }\n"
        ".today { animation: pulse 2.4s ease-in-out 3s infinite; }\n"
        "@keyframes fadeup { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }\n"
        "@keyframes grow { to { transform: scaleX(1); } }\n"
        "@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }"
    )
    body = f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="12" fill="{p["bg"]}" stroke="{p["border"]}"/>\n' + "\n".join(parts)
    return svg_doc(W, H, stats_alt(cfg, s), css, body)


def stats_alt(cfg: dict, s: dict) -> str:
    langs = ", ".join(lang["name"] for lang in s["languages"][:5])
    return (
        f"GitHub stats for {cfg['username']}: {fmt(s['stars'])} stars, {fmt(s['contrib_year'])} contributions "
        f"in the last year ({fmt(s['contrib_all'])} since {s['since']}), {fmt(s['prs'])} pull requests, "
        f"current streak {s['streak_current']} days, longest {s['streak_longest']} days. Top languages: {langs}."
    )


# --------------------------------------------------------------------------- README

def picture(name: str, alt: str) -> str:
    return (
        "<picture>\n"
        f'  <source media="(prefers-color-scheme: dark)" srcset="assets/{name}-dark.svg">\n'
        f'  <source media="(prefers-color-scheme: light)" srcset="assets/{name}-light.svg">\n'
        f'  <img alt="{esc(alt)}" src="assets/{name}-dark.svg" width="100%">\n'
        "</picture>"
    )


def render_readme(cfg: dict, s: dict) -> str:
    about = "\n".join(f"- {line}" for line in cfg["about"])
    links = "\n".join(f"- {link['label']}: [{link['text']}]({link['url']})" for link in cfg.get("links", []))
    terminal_alt = f"{cfg['greeting']} I'm {cfg['name']} (@{cfg['handle']}). " + " ".join(
        "".join(text for _, text in inline_segments(line)) + "." for line in cfg["about"]
    )
    return f"""<!--
  Generated by scripts/build_profile.py from profile.toml — edit those, not this file.
  Refreshed weekly by .github/workflows/update-profile.yml.
-->

<div align="center">

{picture("terminal", terminal_alt)}

{picture("stats", stats_alt(cfg, s))}

</div>

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
        (ASSETS / f"terminal-{theme}.svg").write_text(render_terminal(cfg, palette), encoding="utf-8")
        (ASSETS / f"stats-{theme}.svg").write_text(render_stats(cfg, stats, palette), encoding="utf-8")
    (ROOT / "README.md").write_text(render_readme(cfg, stats), encoding="utf-8")

    summary = {k: v for k, v in stats.items() if k not in ("calendar", "languages")}
    summary["languages"] = [f"{lang['name']} {lang['pct']:.1f}%" for lang in stats["languages"]]
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
