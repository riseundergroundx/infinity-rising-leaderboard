# Infinity Rising Leaderboard

Static leaderboard for Rise Underground, hosted by GitHub Pages and refreshed by GitHub Actions.

## Safety status

The supplied scraper does not yet select the official third-Sunday competition window reliably. Automated deployment is therefore manual-only until that selector is verified. This prevents current-week results from being published under the wrong competition dates.

## Local use

1. Install Python 3.11 or newer.
2. Run `pip install -r requirements.txt`.
3. Run `playwright install chromium`.
4. Run `python scraper/ir_leaderboard_scraper.py --output site/data/placements.csv`.
5. Serve the `site` directory over HTTP.

## Publishing

In GitHub, open **Actions**, select **Update leaderboard**, and run it manually. Enable the schedule in `.github/workflows/update-leaderboard.yml` only after the official date-window selection is implemented and verified.

