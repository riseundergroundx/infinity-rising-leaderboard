"""Daily-round event scraper for the Rise Underground leaderboard.

Each UTC day is one scoring round. Re-running during a day replaces that
day's snapshot instead of double-counting it. Completed rounds are retained
in event_state.json and aggregated into event_scores.json.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

BASE = "https://infinityrising.com/leaderboards"
CALIDO_TRACKS = ["Calido Yellow", "Calido Red", "Calido Purple"]
CALIDO_VEHICLES = [
    "Astro IV", "Bubblejett Bonanza 2023", "Bubblejett Bonanza OG Custom 2023",
    "Bubblejett Sprinter 2022", "Bubblejett Sprinter OG Custom 2022",
    "Bubblejett Super Phantom", "GTi Javelin 2022", "Kazekura Shinobi-X",
    "Rando's Metalworks Sunset Speeder", "Valkyrie F9-R",
    "Valley Raceworx T1-A", "Valley Raceworx T1-B", "Valley Raceworx T1-C",
    "Valley Raceworx T3 2023",
]
AERO_COURSES = range(1, 8)
EXPECTED_BOARDS = 1 + len(AERO_COURSES) + len(CALIDO_TRACKS) * len(CALIDO_VEHICLES)


@dataclass(frozen=True)
class Placement:
    player: str
    board: str
    rank: int
    points: int


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def table_text(page: Page) -> str:
    table = page.locator("table").first
    return table.inner_text() if table.count() else ""


def wait_for_table(page: Page, previous: str | None = None) -> None:
    page.wait_for_selector("table", timeout=20_000)
    if previous:
        try:
            page.wait_for_function(
                "old => (document.querySelector('table')?.innerText || '') !== old",
                arg=previous,
                timeout=8_000,
            )
        except PlaywrightTimeout:
            # Empty boards and unchanged results are legitimate. The delay still
            # prevents reading immediately after a filter click.
            pass
    page.wait_for_timeout(900)


def select_filter(page: Page, group: str, option: str) -> None:
    previous = table_text(page)
    # Start from the option itself. The first ancestor containing a Reset
    # control is the complete filter group; the heading's nearest div is only
    # the heading row on the current Infinity Rising layout.
    target = page.get_by_text(option, exact=True).first
    target.wait_for(state="visible", timeout=12_000)
    section = target.locator(
        "xpath=ancestor::*[self::div or self::section][.//*[normalize-space(text())='Reset']][1]"
    )
    try:
        section.get_by_text("Reset", exact=True).first.click(timeout=3_000)
    except PlaywrightTimeout:
        pass
    target.click(timeout=8_000)
    wait_for_table(page, previous)


def parse_rank(text: str) -> int | None:
    match = re.search(r"(?:^|\s)#?([1-9]|10)(?:\s|$)", text)
    return int(match.group(1)) if match else None


def scrape_table(page: Page, board: str) -> list[Placement]:
    tables = page.locator("table")
    if not tables.count():
        raise RuntimeError(f"No table found for {board}")
    table = tables.first
    headers = [value.strip().lower() for value in table.locator("thead th").all_inner_texts()]
    try:
        player_index = next(i for i, value in enumerate(headers) if "player" in value)
    except StopIteration as exc:
        raise RuntimeError(f"Player column not found for {board}: {headers}") from exc

    found: list[Placement] = []
    for row in table.locator("tbody tr").all():
        cells = row.locator("td").all_inner_texts()
        if len(cells) <= player_index:
            continue
        rank = parse_rank(cells[0]) or parse_rank(row.inner_text())
        player = cells[player_index].strip()
        if rank and player and 1 <= rank <= 10:
            found.append(Placement(player, board, rank, 11 - rank))
        if len(found) == 10:
            break

    ranks = [item.rank for item in found]
    if len(ranks) != len(set(ranks)) or ranks != sorted(ranks):
        raise RuntimeError(f"Invalid or duplicate ranks for {board}: {ranks}")
    if found and ranks[0] != 1:
        raise RuntimeError(f"Leaderboard does not begin at rank 1 for {board}: {ranks}")
    if len(ranks) >= 4 and any(rank not in ranks for rank in range(1, min(10, max(ranks)) + 1)):
        raise RuntimeError(f"Suspicious rank gap for {board}: {ranks}")
    return found


def visit_daily(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    wait_for_table(page)
    select_filter(page, "Timeframe", "Daily")


def scrape_daily() -> tuple[list[Placement], dict[str, int]]:
    placements: list[Placement] = []
    results: dict[str, int] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            board = "Holocache"
            visit_daily(page, f"{BASE}/holocache")
            rows = scrape_table(page, board); placements.extend(rows); results[board] = len(rows)

            for course in AERO_COURSES:
                board = f"Aero Trails — Course {course}"
                visit_daily(page, f"{BASE}/aero-trails?courseId={course}")
                rows = scrape_table(page, board); placements.extend(rows); results[board] = len(rows)

            visit_daily(page, f"{BASE}/calido-valley-raceway")
            for track in CALIDO_TRACKS:
                for vehicle in CALIDO_VEHICLES:
                    board = f"{track} — {vehicle}"
                    select_filter(page, "Track", track)
                    select_filter(page, "Vehicle", vehicle)
                    rows = scrape_table(page, board); placements.extend(rows); results[board] = len(rows)
        finally:
            browser.close()
    return placements, results


def validate(placements: list[Placement], results: dict[str, int]) -> None:
    if len(results) != EXPECTED_BOARDS:
        raise RuntimeError(f"Expected {EXPECTED_BOARDS} boards; completed {len(results)}")
    duplicates = len(placements) - len({(p.player, p.board) for p in placements})
    if duplicates:
        raise RuntimeError(f"Found {duplicates} duplicate player/board placements")
    # A completely empty result across all 50 boards is much more likely to be
    # a broken selector or site outage than a legitimate event snapshot.
    if not placements:
        raise RuntimeError("All 50 boards were empty; preserving the last good result")


def active_event(events: list[dict], now: datetime) -> dict | None:
    for event in events:
        start = datetime.fromisoformat(event["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(event["end"].replace("Z", "+00:00"))
        if start <= now < end:
            return event
    return None


def aggregate(event: dict, rounds: dict[str, list[dict]], now: datetime) -> dict:
    totals: dict[str, dict] = {}
    for day, items in sorted(rounds.items()):
        for item in items:
            player = totals.setdefault(item["player"], {"player": item["player"], "event_score": 0, "firsts": 0, "placements": []})
            player["event_score"] += item["points"]
            player["firsts"] += int(item["rank"] == 1)
            player["placements"].append({"round": day, "board": item["board"], "rank": item["rank"], "points": item["points"]})
    standings = sorted(totals.values(), key=lambda x: (-x["event_score"], -x["firsts"], x["player"].casefold()))
    for index, player in enumerate(standings, start=1):
        player["position"] = index
    start = datetime.fromisoformat(event["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(event["end"].replace("Z", "+00:00"))
    state = "upcoming" if now < start else ("complete" if now >= end else "live")
    return {
        **event,
        "state": state,
        "winner": standings[0]["player"] if state == "complete" and standings else None,
        "standings": standings,
        "rounds_recorded": sorted(rounds),
    }


def write_outputs(args, config: dict, state: dict, placements: list[Placement], results: dict[str, int], now: datetime) -> None:
    event = active_event(config["events"], now)
    if event:
        event_state = state.setdefault("events", {}).setdefault(event["id"], {"rounds": {}})
        event_state["rounds"][now.date().isoformat()] = [asdict(item) for item in placements]
    state["updated_at"] = now.isoformat()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    rendered = []
    for configured in config["events"]:
        rounds = state.get("events", {}).get(configured["id"], {}).get("rounds", {})
        rendered.append(aggregate(configured, rounds, now))
    current = next((item for item in rendered if item["state"] == "live"), None)
    scores = {"generated_at": now.isoformat(), "active_event_id": current["id"] if current else None, "events": rendered}
    args.scores.write_text(json.dumps(scores, indent=2, ensure_ascii=False), encoding="utf-8")

    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["player", "board", "rank", "points"])
        writer.writeheader(); writer.writerows(asdict(item) for item in placements)

    message = f"{current['name']} is live. Updated {now:%Y-%m-%d %H:%M UTC}." if current else "No event is currently live."
    status = {
        "state": "live" if current else "complete", "message": message,
        "scraped_at": now.isoformat(), "successful_boards": len(results),
        "expected_boards": EXPECTED_BOARDS,
    }
    args.status.write_text(json.dumps(status, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("site/data/events.json"))
    parser.add_argument("--state", type=Path, default=Path("site/data/event_state.json"))
    parser.add_argument("--scores", type=Path, default=Path("site/data/event_scores.json"))
    parser.add_argument("--output", type=Path, default=Path("site/data/placements.csv"))
    parser.add_argument("--status", type=Path, default=Path("site/data/status.json"))
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    config = read_json(args.config, {"events": []})
    state = read_json(args.state, {"events": {}})
    if not active_event(config["events"], now):
        # Still rebuild scores so a just-ended event gets its winner.
        write_outputs(args, config, state, [], {}, now)
        return
    placements, results = scrape_daily()
    validate(placements, results)
    write_outputs(args, config, state, placements, results, now)


if __name__ == "__main__":
    main()
