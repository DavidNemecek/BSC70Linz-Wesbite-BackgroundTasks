#!/usr/bin/env python3
"""Fetch BSC 70 Linz team standings from OBV TournamentSoftware.

This is intentionally requests-only so it can run as a cheap scheduled GitHub
Actions task and write a static JSON file.
"""

from __future__ import annotations

import argparse
import dataclasses
import html
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

import requests


BASE_URL = "https://obv.tournamentsoftware.com"
DEFAULT_QUERY = "O\u00d6BV Ligen"
DEFAULT_TEAM_QUERY = "BSC 70 Linz"
DEFAULT_OUTPUT = "data/bsc70-teams.json"
TIMEZONE = "Europe/Vienna"

GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
LEAGUE_LINK_RE = re.compile(
    r'<a\b[^>]*\bhref=["\'](?P<href>/league/(?P<id>'
    + GUID_RE.pattern
    + r'))["\'][^>]*>(?P<body>.*?)</a>',
    re.I | re.S,
)
DRAW_LIST_RE = re.compile(r"var\s+DrawList\s*=\s*(?P<json>\[.*?\])\s*;", re.S)
DATETIME_RE = re.compile(r'datetime=["\'](?P<date>20\d{2}-\d{2}-\d{2})')
SEASON_RE = re.compile(r"\b(20\d{2})\s*[-/]\s*(20\d{2})\b")
INT_RE = re.compile(r"-?\d+")


class ScraperError(RuntimeError):
    pass


@dataclass(frozen=True)
class LeagueCandidate:
    league_id: str
    title: str
    url: str
    first_date: str | None
    last_date: str | None
    season_end_year: int | None


@dataclass(frozen=True)
class DrawInfo:
    draw_id: int
    name: str


@dataclass(frozen=True)
class TeamStanding:
    name: str
    competition: str
    standing: int | None
    played: int | None
    points: int | None
    team_url: str
    table_url: str
    draw_id: int
    row: list[str]


@dataclass(frozen=True)
class ClubInfo:
    name: str
    team_count: int | None
    url: str | None


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return clean_text(" ".join(self.parts))


class StandingTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.current_cell: list[str] = []
        self.current_row: list[str] = []
        self.current_links: list[str] = []
        self.rows: list[tuple[list[str], list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name: value or "" for name, value in attrs}
        if tag == "table":
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.in_row = True
            self.current_row = []
            self.current_links = []
        elif self.in_row and tag in {"td", "th"}:
            self.in_cell = True
            self.current_cell = []
        elif self.in_row and tag == "a":
            href = attrs_dict.get("href")
            if href:
                self.current_links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if self.in_cell and tag in {"td", "th"}:
            self.current_row.append(clean_text(" ".join(self.current_cell)))
            self.current_cell = []
            self.in_cell = False
        elif self.in_row and tag == "tr":
            if self.current_row:
                self.rows.append((self.current_row, self.current_links[:]))
            self.current_row = []
            self.current_links = []
            self.in_row = False
        elif self.in_table and tag == "table":
            self.in_table = False

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.current_cell.append(data)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def shorten_repeated_draw_name(value: str) -> str:
    parts = clean_text(value).split()
    for prefix_len in range(len(parts) // 2, 0, -1):
        if parts[:prefix_len] == parts[prefix_len : prefix_len * 2]:
            return " ".join(parts[:prefix_len] + parts[prefix_len * 2 :])
    return " ".join(parts)


def normalize(value: str | None) -> str:
    text = clean_text(value).casefold()
    for old, new in {"\u00e4": "ae", "\u00f6": "oe", "\u00fc": "ue", "\u00df": "ss"}.items():
        text = text.replace(old, new)
    return clean_text(re.sub(r"[^a-z0-9]+", " ", text))


def html_to_text(markup: str) -> str:
    parser = TextExtractor()
    parser.feed(markup)
    parser.close()
    return parser.text()


def parse_int(value: str | None) -> int | None:
    match = INT_RE.search(value or "")
    return int(match.group(0)) if match else None


def default_search_dates(today: date | None = None) -> tuple[str, str]:
    current_year = (today or date.today()).year
    return f"{current_year - 1}-08-01", f"{current_year + 2}-11-30"


def build_session(timeout: int) -> requests.Session:
    session = requests.Session()
    session.request_timeout = timeout  # type: ignore[attr-defined]
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36 BSC70DataFetch/1.0"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-AT,de;q=0.9,en;q=0.7",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    return session


def get(session: requests.Session, url: str, *, referer: str | None = None, debug: bool = False) -> str:
    headers = {"Referer": referer} if referer else None
    response = session.get(url, headers=headers, timeout=session.request_timeout)  # type: ignore[attr-defined]
    if debug:
        print(f"[GET] {response.status_code} {response.url} ({len(response.text)} bytes)", file=sys.stderr)
    response.raise_for_status()
    return response.text


def post(
    session: requests.Session,
    url: str,
    data: dict[str, str],
    *,
    referer: str | None = None,
    debug: bool = False,
) -> str:
    headers = {"Referer": referer} if referer else None
    response = session.post(url, data=data, headers=headers, timeout=session.request_timeout)  # type: ignore[attr-defined]
    if debug:
        print(f"[POST] {response.status_code} {response.url} ({len(response.text)} bytes)", file=sys.stderr)
    response.raise_for_status()
    return response.text


def extract_dates(markup: str) -> tuple[str | None, str | None]:
    values = [match.group("date") for match in DATETIME_RE.finditer(markup)]
    return (min(values), max(values)) if values else (None, None)


def extract_season_end_year(text: str) -> int | None:
    years = [int(match.group(2)) for match in SEASON_RE.finditer(text)]
    return max(years) if years else None


def league_sort_key(candidate: LeagueCandidate) -> tuple[str, int, str]:
    return (
        candidate.last_date or candidate.first_date or "",
        candidate.season_end_year or 0,
        candidate.title,
    )


def parse_league_candidates(markup: str, query: str) -> list[LeagueCandidate]:
    candidates: dict[str, LeagueCandidate] = {}
    query_norm = normalize(query)
    for match in LEAGUE_LINK_RE.finditer(markup):
        league_id = match.group("id")
        full_anchor = match.group(0)
        title_attr = re.search(r'\btitle=["\'](?P<title>.*?)["\']', full_anchor, re.I | re.S)
        title = clean_text(title_attr.group("title") if title_attr else html_to_text(match.group("body")))
        if query_norm and query_norm not in normalize(title):
            continue
        context = markup[max(0, match.start() - 1200) : min(len(markup), match.end() + 1200)]
        first_date, last_date = extract_dates(context)
        candidate = LeagueCandidate(
            league_id=league_id,
            title=title,
            url=f"{BASE_URL}/league/{league_id}",
            first_date=first_date,
            last_date=last_date,
            season_end_year=extract_season_end_year(title),
        )
        previous = candidates.get(league_id)
        if previous is None or league_sort_key(candidate) > league_sort_key(previous):
            candidates[league_id] = candidate
    return sorted(candidates.values(), key=league_sort_key, reverse=True)


def discover_latest_league(
    session: requests.Session,
    *,
    query: str,
    start_date: str,
    end_date: str,
    max_pages: int,
    debug: bool,
) -> LeagueCandidate:
    search_url = f"{BASE_URL}/find/league?StartDate={start_date}&EndDate={end_date}&page=1&Q={quote(query)}"
    get(session, search_url, debug=debug)

    all_candidates: list[LeagueCandidate] = []
    for page in range(1, max_pages + 1):
        body = post(
            session,
            f"{BASE_URL}/find/league/DoSearch",
            {
                "Page": str(page),
                "LeagueFilter.Q": query,
                "LeagueFilter.StartDate": f"{start_date}T00:00",
                "LeagueFilter.EndDate": f"{end_date}T00:00",
            },
            referer=search_url,
            debug=debug,
        )
        page_candidates = parse_league_candidates(body, query)
        if debug:
            print(f"[league] page={page} candidates={len(page_candidates)}", file=sys.stderr)
        all_candidates.extend(page_candidates)
        if "no-results" in body and not page_candidates:
            break

    unique = {candidate.league_id: candidate for candidate in all_candidates}
    if not unique:
        raise ScraperError("No matching league found in OBV league search.")
    return sorted(unique.values(), key=league_sort_key, reverse=True)[0]


def extract_draws(league_html: str) -> list[DrawInfo]:
    match = DRAW_LIST_RE.search(league_html)
    if not match:
        raise ScraperError("Could not find DrawList on the league page.")
    raw_items = json.loads(match.group("json"))
    draws: list[DrawInfo] = []
    for item in raw_items:
        draw_id = item.get("XTPID")
        name = shorten_repeated_draw_name(item.get("Name"))
        if isinstance(draw_id, int) and name:
            draws.append(DrawInfo(draw_id=draw_id, name=name))
    if not draws:
        raise ScraperError("DrawList was present but empty.")
    return draws


def parse_standings_from_draw(
    markup: str,
    *,
    team_query: str,
    league: LeagueCandidate,
    draw: DrawInfo,
) -> list[TeamStanding]:
    parser = StandingTableParser()
    parser.feed(markup)
    parser.close()

    wanted = normalize(team_query)
    found: list[TeamStanding] = []
    for cells, links in parser.rows:
        team_links = [href for href in links if re.search(r"/league/[^/]+/team/\d+", href, re.I)]
        if len(cells) < 7 or not team_links:
            continue
        team = clean_text(cells[1])
        if wanted and wanted not in normalize(team):
            continue
        found.append(
            TeamStanding(
                name=team,
                competition=draw.name,
                standing=parse_int(cells[0]),
                played=parse_int(cells[2]),
                points=parse_int(cells[6]),
                team_url=urljoin(BASE_URL, team_links[0]),
                table_url=f"{BASE_URL}/league/{league.league_id}/draw/{draw.draw_id}",
                draw_id=draw.draw_id,
                row=cells,
            )
        )
    return found


def extract_club_url_from_team_page(markup: str, league_id: str) -> str | None:
    pattern = re.compile(rf'["\'](?P<href>/league/{re.escape(league_id)}/club/\d+)["\']', re.I)
    match = pattern.search(markup)
    return urljoin(BASE_URL, match.group("href")) if match else None


def extract_club_team_count(markup: str) -> int | None:
    count_match = re.search(r"<strong>\s*(?P<count>\d+)\s*</strong>\s*Teams?\b", markup, re.I)
    if count_match:
        return int(count_match.group("count"))
    team_links = set(re.findall(r"/league/[^/]+/team/\d+", markup, re.I))
    return len(team_links) or None


def fetch_club_info(
    session: requests.Session,
    *,
    standings: list[TeamStanding],
    league: LeagueCandidate,
    debug: bool,
) -> ClubInfo:
    if not standings:
        return ClubInfo(name="BSC 70 Linz", team_count=None, url=None)

    try:
        team_html = get(session, standings[0].team_url, referer=league.url, debug=debug)
        club_url = extract_club_url_from_team_page(team_html, league.league_id)
        if not club_url:
            return ClubInfo(name="BSC 70 Linz", team_count=None, url=None)
        club_html = get(session, club_url, referer=standings[0].team_url, debug=debug)
        team_count = extract_club_team_count(club_html)
        if team_count is None:
            teams_tab_html = get(session, f"{club_url.rstrip('/')}/GetTabContent/teams", referer=club_url, debug=debug)
            team_count = extract_club_team_count(teams_tab_html)
        return ClubInfo(name="BSC 70 Linz", team_count=team_count, url=club_url)
    except requests.RequestException as exc:
        if debug:
            print(f"[club] metadata skipped: {exc}", file=sys.stderr)
        return ClubInfo(name="BSC 70 Linz", team_count=None, url=None)


def build_payload(args: argparse.Namespace) -> dict[str, object]:
    session = build_session(args.timeout)
    if args.league_id:
        league = LeagueCandidate(
            league_id=args.league_id,
            title=args.league_title or args.league_id,
            url=f"{BASE_URL}/league/{args.league_id}",
            first_date=None,
            last_date=None,
            season_end_year=None,
        )
    else:
        league = discover_latest_league(
            session,
            query=args.query,
            start_date=args.start_date,
            end_date=args.end_date,
            max_pages=args.max_pages,
            debug=args.debug,
        )

    league_html = get(session, league.url, debug=args.debug)
    if not args.league_title:
        title_match = re.search(r"<title>(.*?)</title>", league_html, re.I | re.S)
        if title_match and league.title == league.league_id:
            league = dataclasses.replace(league, title=clean_text(html_to_text(title_match.group(1)).split("|")[0]))

    standings: list[TeamStanding] = []
    for draw in extract_draws(league_html):
        draw_url = f"{BASE_URL}/league/{league.league_id}/draw/{draw.draw_id}"
        try:
            draw_html = get(session, draw_url, referer=league.url, debug=args.debug)
        except requests.RequestException as exc:
            if args.debug:
                print(f"[draw] skip {draw.draw_id}: {exc}", file=sys.stderr)
            continue
        standings.extend(parse_standings_from_draw(draw_html, team_query=args.team_query, league=league, draw=draw))

    if not standings:
        raise ScraperError(f"No team matching {args.team_query!r} found in league {league.title}.")

    standings = sorted(standings, key=lambda item: (item.name, item.draw_id))
    club = fetch_club_info(session, standings=standings, league=league, debug=args.debug)
    updated_at = datetime.now(ZoneInfo(TIMEZONE)).replace(microsecond=0).isoformat()
    return {
        "schema_version": 1,
        "updated_at": updated_at,
        "source": BASE_URL,
        "search": {
            "query": args.query,
            "team_query": args.team_query,
            "start_date": args.start_date,
            "end_date": args.end_date,
        },
        "league": {
            "id": league.league_id,
            "title": league.title,
            "url": league.url,
            "first_date": league.first_date,
            "last_date": league.last_date,
        },
        "club": dataclasses.asdict(club),
        "teams": [dataclasses.asdict(item) for item in standings],
    }


def valid_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected date in YYYY-MM-DD format.") from exc
    return value


def parse_args(argv: list[str]) -> argparse.Namespace:
    default_start, default_end = default_search_dates()
    parser = argparse.ArgumentParser(description="Fetch BSC 70 Linz OBV standings JSON.")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="League search query.")
    parser.add_argument("--team-query", default=DEFAULT_TEAM_QUERY, help="Team name filter.")
    parser.add_argument("--start-date", type=valid_date, default=default_start, help=f"Default: {default_start}")
    parser.add_argument("--end-date", type=valid_date, default=default_end, help=f"Default: {default_end}")
    parser.add_argument("--max-pages", type=int, default=3, help="Maximum league search result pages.")
    parser.add_argument("--league-id", help="Skip league discovery and use this league ID.")
    parser.add_argument("--league-title", help="Optional display title when --league-id is used.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output JSON path.")
    parser.add_argument("--stdout", action="store_true", help="Print JSON to stdout instead of writing a file.")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds.")
    parser.add_argument("--debug", action="store_true", help="Print request diagnostics to stderr.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        payload = build_payload(args)
    except (ScraperError, requests.RequestException, json.JSONDecodeError) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1

    output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.stdout:
        print(output, end="")
    else:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8")
        print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
