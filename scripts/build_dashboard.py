#!/usr/bin/env python3
"""Build the Ebola vaccine clinical-development dashboard as a static GitHub Pages site.

Input:
  data/pipeline.csv
  scripts/dashboard_template.html

Output:
  public/index.html
  public/ebola_vaccine_development_pipeline_data.csv
"""
from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_CSV = ROOT / "data" / "pipeline.csv"
TEMPLATE = ROOT / "scripts" / "dashboard_template.html"
PUBLIC = ROOT / "public"
OUT_HTML = PUBLIC / "index.html"
OUT_CSV = PUBLIC / "ebola_vaccine_development_pipeline_data.csv"
REPORT_MD = ROOT / "reports" / "automation_summary.md"
OUT_REPORT = PUBLIC / "automation_summary.md"

REQUIRED_COLUMNS = {
    "id", "target_species", "species_taxon", "candidate", "platform", "developer_sponsor",
    "stage_order", "stage", "status_type", "active_status", "regulatory_status",
    "target_use", "evidence_summary", "next_milestone", "development_geography",
    "source_1", "source_url_1", "source_2", "source_url_2", "source_3", "source_url_3", "notes",
}


def utc_build_stamp() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.day} {now.strftime('%B %Y, %H:%M UTC')}"


def read_pipeline() -> list[dict[str, object]]:
    if not DATA_CSV.exists():
        raise FileNotFoundError(f"Missing {DATA_CSV}")
    with DATA_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing required CSV columns: {', '.join(sorted(missing))}")
        rows: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for row in reader:
            row = {k: (v or "").strip() for k, v in row.items()}
            if not row.get("id"):
                raise ValueError("Every row must have a non-empty id")
            if row["id"] in seen_ids:
                raise ValueError(f"Duplicate id: {row['id']}")
            seen_ids.add(row["id"])
            try:
                row["stage_order"] = int(str(row["stage_order"]).strip())
            except ValueError as exc:
                raise ValueError(f"stage_order must be an integer for id={row['id']}") from exc
            rows.append(row)
    if not rows:
        raise ValueError("data/pipeline.csv contains no rows")
    return rows


def main() -> None:
    PUBLIC.mkdir(exist_ok=True)
    rows = read_pipeline()
    template = TEMPLATE.read_text(encoding="utf-8")
    html = template.replace("@@DATA_JSON@@", json.dumps(rows, ensure_ascii=False, indent=2))
    html = html.replace("@@BUILD_DATE@@", utc_build_stamp())
    OUT_HTML.write_text(html, encoding="utf-8")
    shutil.copy2(DATA_CSV, OUT_CSV)
    if REPORT_MD.exists():
        shutil.copy2(REPORT_MD, OUT_REPORT)
    print(f"Built {OUT_HTML.relative_to(ROOT)} with {len(rows)} pipeline rows")


if __name__ == "__main__":
    main()
