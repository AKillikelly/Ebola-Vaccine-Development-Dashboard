# Fully Automated Ebola Vaccine Development Dashboard

This repository builds and deploys a **fully automated** GitHub Pages dashboard that maps Ebola vaccine development along the clinical-development pathway for:

- Zaire
- Sudan
- Taï Forest
- Bundibugyo
- Reston
- Bombali

The dashboard is **not** a case map. It is a strain-by-strain vaccine pipeline map.

## What is automated?

Every scheduled run performs the full pipeline without manual data entry:

```text
Public sources → scripts/fetch_pipeline.py → data/pipeline.csv → scripts/build_dashboard.py → public/index.html → GitHub Pages
```

The automation:

1. Pulls Ebola vaccine trial records from the ClinicalTrials.gov v2 API.
2. Deduplicates studies by NCT ID.
3. Classifies candidate/platform/species using deterministic text rules.
4. Converts clinical-trial phase fields into pathway stages.
5. Checks FDA, CDC, openFDA, EMA, WHO, CEPI, sponsor, and EBOVAC pages for configured regulatory and development signals.
6. Saves GDELT discovery articles as audit material, but does not use news articles to upstage candidates.
7. Generates gap rows for strains where no dedicated pathway signal is found.
8. Rebuilds and deploys the dashboard.

## Repository structure

```text
.github/workflows/pages.yml          # Weekly automated source pull, build, deploy
scripts/fetch_pipeline.py            # Public-source collector and deterministic pipeline classifier
scripts/build_dashboard.py           # Converts generated CSV into public/index.html
scripts/dashboard_template.html      # Static dashboard template
data/pipeline.csv                    # Bootstrap/generated pipeline data
reports/automation_summary.md        # Generated audit report
public/index.html                    # Built dashboard for GitHub Pages
```

## Deploy to GitHub Pages

Create a new repository, then push this project:

```bash
git init
git add .
git commit -m "Initial fully automated Ebola vaccine dashboard"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/ebola-vaccine-dashboard.git
git push -u origin main
```

In GitHub:

```text
Settings → Pages → Build and deployment → Source → GitHub Actions
```

Then run:

```text
Actions → Automated refresh and deploy dashboard → Run workflow
```

## Weekly schedule

The workflow runs every Monday at 06:17 in the `America/Montreal` timezone:

```yaml
schedule:
  - cron: "17 6 * * 1"
    timezone: "America/Montreal"
```

The workflow also runs on pushes to `main` and can be triggered manually from the Actions tab.

## Optional: commit generated snapshots back to the repo

By default, the dashboard is fully automated and publishes the generated CSV/report inside the GitHub Pages artifact, but it does **not** commit generated data back to the repository.

To keep a versioned data history inside GitHub, create this repository variable:

```text
Settings → Secrets and variables → Actions → Variables → New repository variable
```

Name:

```text
COMMIT_GENERATED_DATA
```

Value:

```text
true
```

When enabled, scheduled/manual runs commit:

```text
data/pipeline.csv
data/raw/
reports/automation_summary.md
public/ebola_vaccine_development_pipeline_data.csv
public/automation_summary.md
```

## Automation limits

This project deliberately avoids arbitrary webpage interpretation. It can automatically update structured and configured signals, but it will not infer a new regulatory or clinical stage from a random press article. News discovery is saved for audit only.

The clinical pathway stage should therefore be read as an **automated, conservative signal**, not as regulatory advice.

## Local build

```bash
python scripts/fetch_pipeline.py
python scripts/build_dashboard.py
python -m http.server 8000 --directory public
```

Open:

```text
http://localhost:8000
```

## Data columns

The generated CSV includes the dashboard fields plus audit columns:

```text
id,target_species,species_taxon,candidate,platform,developer_sponsor,
stage_order,stage,status_type,active_status,regulatory_status,target_use,
evidence_summary,next_milestone,development_geography,
source_1,source_url_1,source_2,source_url_2,source_3,source_url_3,notes,
automation_method,last_checked_utc,supporting_record_count
```

Stage order convention:

| stage_order | Development meaning |
|---:|---|
| 0 | No dedicated human-vaccine pathway / gap lane |
| 1 | Discovery / translational research |
| 2 | Preclinical or manufacturing-enabling |
| 3 | Phase 1 |
| 4 | Phase 2 |
| 5 | Outbreak efficacy / Phase 3 |
| 6 | Regulatory authorization / prequalification |
| 7 | Stockpile / deployed / post-licensure |
