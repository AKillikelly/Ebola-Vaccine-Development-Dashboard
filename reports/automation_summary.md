# Ebola vaccine pipeline automation summary

This repository is configured for fully automated data refreshes. The included `data/pipeline.csv` is a bootstrap snapshot so the dashboard can render immediately after upload. The first scheduled or manual GitHub Actions run executes `scripts/fetch_pipeline.py`, overwrites `data/pipeline.csv` from public sources, rebuilds the dashboard, and deploys GitHub Pages.
