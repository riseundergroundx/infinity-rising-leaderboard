"""Scrape Infinity Rising placements with validation and fail-closed publishing.

The official competition period must be supplied as a URL template after its
real query parameters are confirmed. The scraper refuses to publish otherwise.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
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


def competition_window(now: datetime) -> tuple[datetime, datetime]:
    first = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    start = first_sunday + timedelta(days=14)
    return start, start + timedelta(days=7) - timedelta(seconds=1)


def period_url(base_url: str, start: datetime, end: datetime) -> str:
    template = os.getenv("IR_PERIOD_URL_TEMPLATE", "").strip()
    if not template:
        raise RuntimeError(
            "IR_PERIOD_URL_TEMPLATE is not configured. Refusing to publish a generic "
            "rolling-week scrape under the official monthly competition dates."
        )
    return template.format(
        base=base_url,
        start=start.isoformat().replace("+00:00", "Z"),
        end=end.isoformat().replace("+00:00", "Z"),
    )


def wait_for_changed_table(page: Page, previous: str | None = None) -> None:
    page.wait_for_selector("table", timeout=20_000)
    if previous is not None:
        page.wait_for_function(
            "old => (document.querySelector('table')?.innerText || '') !== old",
            previous,
            timeout=20_000,
        )
    page.wait_for_timeout(500)


def table_text(page: Page) -> str:
    table = page.locator("table").first
    return table.inner_text() if table.count() else ""


def select_filter(page: Page, group: str, option: str) -> None:
    previous = table_text(page)
    heading = page.get_by_text(group, exact=True).first
    section = heading.locator("xpath=ancestor::*[self::div or self::section][1]")
    try:
        section.get_by_text("Reset", exact=True).first.click(timeout=3_000)
    except PlaywrightTimeout:
        pass
    target = section.get_by_text(option, exact=True).first
    target.click(timeout=8_000)
    wait_for_changed_table(page, previous)


def parse_rank(text: str) -> int | None:
    match = re.search(r"(?:^|\s)#?([1-9]|10)(?:\s|$)", text)
    return int(match.group(1)) if match else None


def scrape_table(page: Page, board: str) -> list[Placement]:
    tables = page.locator("table")
    if not tables.count():
        raise RuntimeError(f"No table found for {board}")
    table = tables.first
    headers = [x.strip().lower() for x in table.locator("thead th").all_inner_texts()]
    try:
        player_index = next(i for i, value in enumerate(headers) if "player" in value)
    except StopIteration as exc:
        raise RuntimeError(f"Player column not found for {board}: {headers}") from exc

    placements: list[Placement] = []
    for row in table.locator("tbody tr").all():
        cells = row.locator("td").all_inner_texts()
        if len(cells) <= player_index:
            continue
        rank = parse_rank(cells[0]) or parse_rank(row.inner_text())
        player = cells[player_index].strip()
        if rank and player and 1 <= rank <= 10:
            placements.append(Placement(player, board, rank, 11 - rank))
        if len(placements) == 10:
            break

    ranks = [p.rank for p in placements]
    if len(ranks) != len(set(ranks)) or ranks != sorted(ranks):
        raise RuntimeError(f"Invalid or duplicate ranks for {board}: {ranks}")
    if placements and ranks[0] != 1:
        raise RuntimeError(f"Leaderboard does not begin at rank 1 for {board}: {ranks}")
    return placements


def visit(page: Page, url: str, start: datetime, end: datetime) -> None:
    page.goto(period_url(url, start, end), wait_until="domcontentloaded", timeout=30_000)
    wait_for_changed_table(page)


def scrape(start: datetime, end: datetime) -> tuple[list[Placement], dict[str, str]]:
    placements: list[Placement] = []
    results: dict[str, str] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            board = "Holocache"
            visit(page, f"{BASE}/holocache", start, end)
            found = scrape_table(page, board); placements.extend(found); results[board] = f"success:{len(found)}"

            for course in AERO_COURSES:
                board = f"Aero Trails — Course {course}"
                visit(page, f"{BASE}/aero-trails?courseId={course}", start, end)
                found = scrape_table(page, board); placements.extend(found); results[board] = f"success:{len(found)}"

            visit(page, f"{BASE}/calido-valley-raceway", start, end)
            for track in CALIDO_TRACKS:
                for vehicle in CALIDO_VEHICLES:
                    board = f"{track} — {vehicle}"
                    select_filter(page, "Track", track)
                    select_filter(page, "Vehicle", vehicle)
                    found = scrape_table(page, board); placements.extend(found); results[board] = f"success:{len(found)}"
        finally:
            browser.close()
    return placements, results


def validate(placements: list[Placement], results: dict[str, str]) -> None:
    if len(results) != EXPECTED_BOARDS:
        raise RuntimeError(f"Expected {EXPECTED_BOARDS} boards; completed {len(results)}")
    duplicates = len(placements) - len({(p.player, p.board) for p in placements})
    if duplicates:
        raise RuntimeError(f"Found {duplicates} duplicate player/board placements")
    by_board: dict[str, list[int]] = defaultdict(list)
    for item in placements:
        by_board[item.board].append(item.rank)
    for board, ranks in by_board.items():
        if len(ranks) >= 4 and any(rank not in ranks for rank in range(1, min(10, max(ranks)) + 1)):
            raise RuntimeError(f"Suspicious rank gap for {board}: {sorted(ranks)}")


def write_outputs(output: Path, status_path: Path, placements: list[Placement], results: dict[str, str], start: datetime, end: datetime) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["player", "board", "rank", "points"])
        writer.writeheader(); writer.writerows(asdict(item) for item in placements)
    status = {
        "state": "verified",
        "message": f"Verified data from {len(results)} boards. Updated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}.",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "competition_start": start.isoformat(), "competition_end": end.isoformat(),
        "successful_boards": len(results), "expected_boards": EXPECTED_BOARDS,
    }
    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("site/data/placements.csv"))
    parser.add_argument("--status", type=Path, default=Path("site/data/status.json"))
    args = parser.parse_args()
    start, end = competition_window(datetime.now(timezone.utc))
    placements, results = scrape(start, end)
    validate(placements, results)
    write_outputs(args.output, args.status, placements, results, start, end)


if __name__ == "__main__":
    main()

