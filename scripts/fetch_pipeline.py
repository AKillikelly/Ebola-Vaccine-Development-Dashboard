#!/usr/bin/env python3
"""Fully automated Ebola vaccine pipeline data builder.

This script collects public signals from structured and semi-structured sources and
writes data/pipeline.csv for the static dashboard. It is intentionally conservative:
ClinicalTrials.gov phases are used for clinical-stage placement; regulator pages/APIs
are used for authorization placement; selected WHO/CEPI pages are used only for
preclinical/clinical-enabling signals when the candidate and species terms are present.

No manual weekly curation is required. The trade-off is that arbitrary webpages are not
trusted enough to upstage a candidate unless a deterministic rule below matches.
"""
from __future__ import annotations

import csv
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
REPORTS = ROOT / "reports"
OUT_CSV = DATA_DIR / "pipeline.csv"
AUTOMATION_REPORT = REPORTS / "automation_summary.md"

USER_AGENT = "ebola-vaccine-pipeline-dashboard/2.0 (+https://github.com/)"
TIMEOUT_SECONDS = 35

CTG_ENDPOINT = "https://clinicaltrials.gov/api/v2/studies"
OPENFDA_LABEL_ENDPOINT = "https://api.fda.gov/drug/label.json"

CLINICALTRIALS_QUERIES = [
    "Ebola vaccine",
    "Ebolavirus vaccine",
    "Zaire ebolavirus vaccine",
    "Sudan ebolavirus vaccine",
    "Bundibugyo ebolavirus vaccine",
    "Reston ebolavirus vaccine",
    "Bombali ebolavirus vaccine",
    "ERVEBO",
    "V920 Ebola",
    "rVSV ZEBOV",
    "rVSV Sudan Ebola vaccine",
    "Tokemeza Sudan virus vaccine",
    "cAd3 Sudan ebolavirus vaccine",
    "Sabin Sudan ebolavirus vaccine",
    "Ad26.ZEBOV MVA-BN-Filo",
    "Zabdeno Mvabea",
    "ChAdOx1 Ebola vaccine",
]

WATCH_PAGES = {
    "cdc_ervebo": "https://www.cdc.gov/ebola/hcp/vaccines/index.html",
    "fda_ervebo": "https://www.fda.gov/vaccines-blood-biologics/ervebo",
    "ema_ervebo": "https://www.ema.europa.eu/en/medicines/human/EPAR/ervebo",
    "ema_zabdeno": "https://www.ema.europa.eu/en/medicines/human/EPAR/zabdeno",
    "ema_mvabea": "https://www.ema.europa.eu/en/medicines/human/EPAR/mvabea",
    "who_sudan_trial": "https://www.who.int/news/item/03-02-2025-groundbreaking-ebola-vaccination-trial-launches-today-in-uganda",
    "sabin_sudan": "https://www.sabin.org/vaccine-research-and-development/sudan-ebolavirus-vaccine-development-program/",
    "sabin_phase2": "https://www.sabin.org/resources/sabin-vaccine-institute-completes-enrollment-in-all-phase-2-clinical-trials-for-cad3-marburg-vaccine-and-cad3-sudan-ebolavirus-vaccine/",
    "who_bundibugyo": "https://www.who.int/news/item/28-05-2026-experts-convened-by-who-advise-on-candidate-treatments-and-vaccines-for-ebola-disease-caused-by-bundibugyo-virus",
    "cepi_bundibugyo": "https://cepi.net/cepi-fast-tracks-three-bundibugyo-ebolavirus-vaccine-candidates",
    "ebovac_platform": "https://www.ebovac.org/sample-page/vaccine-development/",
    "cdc_basics": "https://www.cdc.gov/ebola/about/index.html",
}

GDELT_DISCOVERY_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_DISCOVERY_QUERIES = [
    '"Ebola vaccine" clinical trial',
    '"Sudan virus" vaccine trial',
    '"Bundibugyo" Ebola vaccine',
    '"Ebola vaccine" authorization',
]

SPECIES_TAXON = {
    "Zaire": "Orthoebolavirus zairense",
    "Sudan": "Orthoebolavirus sudanense",
    "Taï Forest": "Orthoebolavirus taiense",
    "Bundibugyo": "Orthoebolavirus bundibugyoense",
    "Reston": "Orthoebolavirus restonense",
    "Bombali": "Orthoebolavirus bombaliense",
}

STAGE_LABEL = {
    0: "No dedicated clinical pathway identified",
    1: "Discovery / translational research",
    2: "Preclinical + CMC / clinical-enabling",
    3: "Phase 1 safety / immunogenicity",
    4: "Phase 2 dose / safety / immunogenicity",
    5: "Outbreak efficacy / Phase 3",
    6: "Regulatory authorization / prequalification",
    7: "Stockpile / deployed / post-licensure",
}

REQUIRED_COLUMNS = [
    "id", "target_species", "species_taxon", "candidate", "platform", "developer_sponsor",
    "stage_order", "stage", "status_type", "active_status", "regulatory_status",
    "target_use", "evidence_summary", "next_milestone", "development_geography",
    "source_1", "source_url_1", "source_2", "source_url_2", "source_3", "source_url_3", "notes",
    "automation_method", "last_checked_utc", "supporting_record_count",
]

warnings: list[str] = []
source_status: dict[str, dict[str, Any]] = {}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    value = value.lower()
    value = value.replace("ï", "i").replace("é", "e").replace("∆", "d")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "row"


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def fetch_bytes(url: str, *, params: dict[str, str] | None = None, label: str | None = None) -> bytes | None:
    full_url = url
    if params:
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html,*/*"})
    started = datetime.now(timezone.utc)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read()
            source_status[label or full_url] = {
                "url": full_url,
                "status": getattr(resp, "status", None),
                "bytes": len(body),
                "fetched_utc": started.isoformat(),
                "error": None,
            }
            return body
    except urllib.error.HTTPError as exc:
        msg = f"HTTP {exc.code} for {label or full_url}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001 - record external source failure and continue
        msg = f"{type(exc).__name__} for {label or full_url}: {exc}"
    warnings.append(msg)
    source_status[label or full_url] = {
        "url": full_url,
        "status": None,
        "bytes": 0,
        "fetched_utc": started.isoformat(),
        "error": msg,
    }
    return None


def fetch_json(url: str, *, params: dict[str, str] | None = None, label: str | None = None) -> Any | None:
    body = fetch_bytes(url, params=params, label=label)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"JSON parse failure for {label or url}: {exc}")
        return None


def fetch_text(url: str, *, label: str | None = None) -> str:
    body = fetch_bytes(url, label=label)
    if body is None:
        return ""
    for encoding in ("utf-8", "latin-1"):
        try:
            return clean_text(body.decode(encoding, errors="replace"))
        except Exception:
            continue
    return clean_text(body.decode("utf-8", errors="replace"))


def deep_get(obj: dict[str, Any], path: list[str], default: Any = None) -> Any:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def unique_preserve(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        value = value.strip()
        if not value or value.lower() in seen:
            continue
        out.append(value)
        seen.add(value.lower())
    return out


@dataclass
class CandidatePattern:
    canonical: str
    platform: str
    default_species: list[str]
    patterns: list[str]
    sponsor_hint: str = ""


CANDIDATE_PATTERNS = [
    CandidatePattern(
        "Ervebo / rVSV-ZEBOV (V920)",
        "Replication-competent recombinant VSV vector expressing Zaire ebolavirus glycoprotein",
        ["Zaire"],
        [r"\bervebo\b", r"\bv920\b", r"rvsv[^\n,;]{0,50}zebov", r"zebov[^\n,;]{0,50}gp", r"ebola zaire vaccine"],
        "Merck/MSD",
    ),
    CandidatePattern(
        "Zabdeno + Mvabea / Ad26.ZEBOV + MVA-BN-Filo",
        "Heterologous prime-boost Ad26 vector + MVA vector regimen",
        ["Zaire"],
        [r"\bzabdeno\b", r"\bmvabea\b", r"ad26\.zebov", r"mva[- ]bn[- ]filo", r"ad26[^\n,;]{0,40}zebov"],
        "Janssen / Johnson & Johnson; Bavarian Nordic",
    ),
    CandidatePattern(
        "IAVI rVSV-Sudan / Tokemeza SVD",
        "Replication-competent recombinant VSV vector expressing Sudan virus glycoprotein",
        ["Sudan"],
        [r"tokemeza", r"rvsv[^\n,;]{0,50}(sudan|sudv|sebov)", r"iavi[^\n]{0,80}sudan[^\n]{0,80}vaccine"],
        "IAVI",
    ),
    CandidatePattern(
        "Sabin cAd3-Sudan ebolavirus vaccine",
        "Chimpanzee adenovirus type 3 vector",
        ["Sudan"],
        [r"cad3[^\n,;]{0,50}sudan", r"sabin[^\n]{0,80}sudan[^\n]{0,80}vaccine"],
        "Sabin Vaccine Institute",
    ),
    CandidatePattern(
        "ChAdOx1 biEBOV",
        "Replication-deficient ChAdOx1 adenoviral vector encoding Zaire and Sudan glycoproteins",
        ["Zaire", "Sudan"],
        [r"chadox1[^\n,;]{0,50}(biebov|ebola)", r"biebov"],
        "University of Oxford / Jenner Institute",
    ),
]


def study_text(study: dict[str, Any]) -> str:
    protocol = study.get("protocolSection", {})
    modules = [
        deep_get(protocol, ["identificationModule", "briefTitle"], ""),
        deep_get(protocol, ["identificationModule", "officialTitle"], ""),
        " ".join(as_list(deep_get(protocol, ["conditionsModule", "conditions"], []))),
    ]
    for item in as_list(deep_get(protocol, ["armsInterventionsModule", "interventions"], [])):
        if isinstance(item, dict):
            modules.append(str(item.get("name") or ""))
            modules.append(str(item.get("description") or ""))
    return clean_text(" ".join(str(x) for x in modules))


def candidate_from_text(text: str) -> tuple[str, str, list[str], str]:
    lower = text.lower()
    for cp in CANDIDATE_PATTERNS:
        if any(re.search(pat, lower, flags=re.I) for pat in cp.patterns):
            return cp.canonical, cp.platform, list(cp.default_species), cp.sponsor_hint

    # Fallback to the most specific intervention-like label in the text. This keeps
    # newly registered candidates visible even before a dedicated rule is added.
    fallback = "Unclassified Ebola vaccine candidate"
    platform = "Vaccine platform not classified by automated parser"
    if "mrna" in lower:
        platform = "mRNA vaccine platform"
    elif "adenovirus" in lower or "ad26" in lower or "chadox" in lower or "cad3" in lower:
        platform = "Adenoviral vector platform"
    elif "rvsv" in lower or "vesicular stomatitis" in lower:
        platform = "Recombinant VSV vector platform"
    elif "mva" in lower or "vaccinia" in lower:
        platform = "MVA/vaccinia vector platform"
    return fallback, platform, [], ""


def species_from_text(text: str, default_species: list[str]) -> list[str]:
    lower = text.lower().replace("taï", "tai")
    species: list[str] = []
    if re.search(r"\b(sudan|sudv|sudv-|sebov|sudan virus)\b", lower):
        species.append("Sudan")
    if re.search(r"\b(bundibugyo|bdbv)\b", lower):
        species.append("Bundibugyo")
    if re.search(r"\b(tai forest|taie|tafv|taï forest)\b", lower):
        species.append("Taï Forest")
    if re.search(r"\b(reston|restv)\b", lower):
        species.append("Reston")
    if re.search(r"\b(bombali|bombv)\b", lower):
        species.append("Bombali")
    if re.search(r"\b(zaire|zebov|ebov-gp|ebov gp|ebola zaire|ervebo|v920|ad26\.zebov|zebov-gp)\b", lower):
        species.append("Zaire")
    if not species:
        species.extend(default_species)
    return unique_preserve([s for s in species if s in SPECIES_TAXON])


def is_probable_vaccine_study(text: str) -> bool:
    lower = text.lower()
    has_ebola = any(term in lower for term in ["ebola", "ebolavirus", "zebov", "sudv", "bundibugyo", "reston", "bombali"])
    has_vaccine = any(term in lower for term in ["vaccine", "vaccination", "immunization", "immunogenicity", "rvsv", "ad26", "mva-bn", "cad3", "chadox", "ervebo", "zabdeno", "mvabea"])
    return has_ebola and has_vaccine


def phase_to_stage(phases: list[str]) -> tuple[int, str]:
    joined = " ".join(phases).upper()
    if "PHASE4" in joined:
        return 7, "Phase 4 / post-licensure study"
    if "PHASE3" in joined:
        return 5, "Phase 3 / efficacy study"
    if "PHASE2" in joined:
        return 4, "Phase 2 safety / immunogenicity study"
    if "PHASE1" in joined or "EARLY_PHASE1" in joined:
        return 3, "Phase 1 safety / immunogenicity study"
    return 3, "Clinical study; phase not classified in registry"


def get_nct_url(nct_id: str) -> str:
    return f"https://clinicaltrials.gov/study/{nct_id}"


def fetch_clinicaltrials() -> list[dict[str, Any]]:
    studies_by_nct: dict[str, dict[str, Any]] = {}
    for query in CLINICALTRIALS_QUERIES:
        page_token = ""
        for _page in range(20):  # defensive ceiling; Ebola vaccine result sets are small
            params = {
                "format": "json",
                "pageSize": "100",
                "query.term": query,
            }
            if page_token:
                params["pageToken"] = page_token
            payload = fetch_json(CTG_ENDPOINT, params=params, label=f"ClinicalTrials.gov: {query}")
            if not payload:
                break
            for study in payload.get("studies", []) or []:
                nct_id = deep_get(study, ["protocolSection", "identificationModule", "nctId"], "")
                if nct_id:
                    studies_by_nct[nct_id] = study
            page_token = payload.get("nextPageToken") or ""
            if not page_token:
                break
            time.sleep(0.2)
        time.sleep(0.2)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / "clinicaltrials_studies.json").write_text(json.dumps(studies_by_nct, indent=2, sort_keys=True), encoding="utf-8")
    return list(studies_by_nct.values())


@dataclass
class AggregatedCandidate:
    species: str
    candidate: str
    platform: str
    sponsor_hint: str = ""
    max_stage_order: int = 0
    max_stage_detail: str = ""
    nct_ids: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    sponsors: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    last_updates: list[str] = field(default_factory=list)
    source_urls: list[tuple[str, str]] = field(default_factory=list)
    method: str = "ClinicalTrials.gov v2 API; deterministic phase/species parser"

    def add_trial(self, study: dict[str, Any], stage_order: int, stage_detail: str) -> None:
        protocol = study.get("protocolSection", {})
        nct_id = deep_get(protocol, ["identificationModule", "nctId"], "")
        brief = deep_get(protocol, ["identificationModule", "briefTitle"], "")
        official = deep_get(protocol, ["identificationModule", "officialTitle"], "")
        status = deep_get(protocol, ["statusModule", "overallStatus"], "")
        phases = as_list(deep_get(protocol, ["designModule", "phases"], []))
        lead = deep_get(protocol, ["sponsorCollaboratorsModule", "leadSponsor", "name"], "")
        collaborators = [c.get("name", "") for c in as_list(deep_get(protocol, ["sponsorCollaboratorsModule", "collaborators"], [])) if isinstance(c, dict)]
        last_update = deep_get(protocol, ["statusModule", "lastUpdatePostDateStruct", "date"], "")
        locations = as_list(deep_get(protocol, ["contactsLocationsModule", "locations"], []))
        countries = [loc.get("country", "") for loc in locations if isinstance(loc, dict)]
        if nct_id and nct_id not in self.nct_ids:
            self.nct_ids.append(nct_id)
            self.source_urls.append((nct_id, get_nct_url(nct_id)))
        self.titles.extend([brief or official])
        self.statuses.extend([status])
        self.phases.extend([str(p) for p in phases])
        self.sponsors.extend([lead, *collaborators])
        self.countries.extend(countries)
        self.last_updates.extend([last_update])
        if stage_order > self.max_stage_order:
            self.max_stage_order = stage_order
            self.max_stage_detail = stage_detail


def aggregate_clinical_rows(studies: list[dict[str, Any]]) -> dict[tuple[str, str], AggregatedCandidate]:
    candidates: dict[tuple[str, str], AggregatedCandidate] = {}
    for study in studies:
        text = study_text(study)
        if not is_probable_vaccine_study(text):
            continue
        protocol = study.get("protocolSection", {})
        phases = [str(p) for p in as_list(deep_get(protocol, ["designModule", "phases"], []))]
        stage_order, stage_detail = phase_to_stage(phases)
        candidate, platform, default_species, sponsor_hint = candidate_from_text(text)
        species_list = species_from_text(text, default_species)
        if not species_list:
            # Keep broad/unclassified Ebola vaccine studies visible in the Zaire lane only
            # when a Zaire-specific term is likely but absent from structured fields.
            if candidate != "Unclassified Ebola vaccine candidate":
                species_list = default_species
        for species in species_list:
            key = (species, candidate)
            if key not in candidates:
                candidates[key] = AggregatedCandidate(species, candidate, platform, sponsor_hint=sponsor_hint)
            candidates[key].add_trial(study, stage_order, stage_detail)
    return candidates


def row_from_aggregate(agg: AggregatedCandidate) -> dict[str, Any]:
    titles = unique_preserve([t for t in agg.titles if t])[:3]
    statuses = unique_preserve([s.replace("_", " ").title() for s in agg.statuses if s])
    phases = unique_preserve([p.replace("_", " ").title() for p in agg.phases if p])
    sponsors = unique_preserve([s for s in agg.sponsors if s])
    if agg.sponsor_hint:
        sponsors = unique_preserve([agg.sponsor_hint, *sponsors])
    countries = unique_preserve([c for c in agg.countries if c])
    last_updates = sorted([d for d in agg.last_updates if d])
    newest = last_updates[-1] if last_updates else "not reported"
    sources = agg.source_urls[:3]
    source_fields = {}
    for idx in range(3):
        if idx < len(sources):
            source_fields[f"source_{idx + 1}"] = f"ClinicalTrials.gov {sources[idx][0]}"
            source_fields[f"source_url_{idx + 1}"] = sources[idx][1]
        else:
            source_fields[f"source_{idx + 1}"] = ""
            source_fields[f"source_url_{idx + 1}"] = ""

    stage_order = max(agg.max_stage_order, 3)
    stage = STAGE_LABEL.get(stage_order, agg.max_stage_detail)
    phase_text = ", ".join(phases) if phases else "registry phase not classified"
    status_text = "; ".join(statuses) if statuses else "Registry status not reported"
    return {
        "id": f"{slugify(agg.species)}-{slugify(agg.candidate)}-ctg",
        "target_species": agg.species,
        "species_taxon": SPECIES_TAXON[agg.species],
        "candidate": agg.candidate,
        "platform": agg.platform,
        "developer_sponsor": "; ".join(sponsors[:5]) or "Not classified by automated parser",
        "stage_order": stage_order,
        "stage": stage,
        "status_type": "Automated clinical-trial signal",
        "active_status": status_text,
        "regulatory_status": "Investigational unless a separate automated regulatory row or source indicates authorization.",
        "target_use": f"Human clinical development for {agg.species} ebolavirus disease or related exposure risk.",
        "evidence_summary": f"Automated parser found {len(agg.nct_ids)} ClinicalTrials.gov record(s). Highest registry phase signal: {phase_text}. Newest registry update: {newest}. Example title(s): {' | '.join(titles)}",
        "next_milestone": "Monitor trial completion, results posting, phase advancement, and any regulator or WHO stockpile/prequalification signal.",
        "development_geography": "; ".join(countries[:8]) or "ClinicalTrials.gov locations not reported in parsed record(s)",
        **source_fields,
        "notes": "Generated automatically from ClinicalTrials.gov v2. Species and candidate labels are rule-based and should be treated as an auditable automation output.",
        "automation_method": agg.method,
        "last_checked_utc": now_utc_iso(),
        "supporting_record_count": len(agg.nct_ids),
    }


def make_source_fields(sources: list[tuple[str, str]]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for i in range(3):
        if i < len(sources):
            fields[f"source_{i + 1}"] = sources[i][0]
            fields[f"source_url_{i + 1}"] = sources[i][1]
        else:
            fields[f"source_{i + 1}"] = ""
            fields[f"source_url_{i + 1}"] = ""
    return fields


def add_or_upgrade(rows: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
    row_id = str(row["id"])
    existing = rows.get(row_id)
    if not existing:
        rows[row_id] = row
        return
    if int(row.get("stage_order", 0)) >= int(existing.get("stage_order", 0)):
        rows[row_id] = row


def fetch_watch_pages() -> dict[str, str]:
    pages: dict[str, str] = {}
    for label, url in WATCH_PAGES.items():
        pages[label] = fetch_text(url, label=f"watch_page:{label}")
        time.sleep(0.4)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / "watch_pages_text.json").write_text(json.dumps({k: v[:20000] for k, v in pages.items()}, indent=2), encoding="utf-8")
    return pages


def fetch_openfda_ervebo() -> dict[str, Any] | None:
    params = {"search": 'openfda.brand_name:"ERVEBO"', "limit": "1"}
    payload = fetch_json(OPENFDA_LABEL_ENDPOINT, params=params, label="openFDA label: ERVEBO")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / "openfda_ervebo_label.json").write_text(json.dumps(payload or {}, indent=2), encoding="utf-8")
    return payload if isinstance(payload, dict) else None


def regulatory_and_watch_rows(pages: dict[str, str], openfda_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    checked = now_utc_iso()

    cdc = pages.get("cdc_ervebo", "")
    fda = pages.get("fda_ervebo", "")
    label_text = ""
    if openfda_payload and openfda_payload.get("results"):
        first = openfda_payload["results"][0]
        label_text = " ".join(first.get("indications_and_usage", []) or [])
    approval_signal = "zaire" in (cdc + fda + label_text).lower() and any(term in (cdc + fda + label_text).lower() for term in ["approved", "indicated", "prevention"])
    if approval_signal:
        rows.append({
            "id": "zaire-ervebo-regulatory-auto",
            "target_species": "Zaire",
            "species_taxon": SPECIES_TAXON["Zaire"],
            "candidate": "Ervebo / rVSV-ZEBOV (V920)",
            "platform": "Replication-competent recombinant VSV vector expressing Zaire ebolavirus glycoprotein",
            "developer_sponsor": "Merck/MSD",
            "stage_order": 7,
            "stage": STAGE_LABEL[7],
            "status_type": "Automated regulatory/product signal",
            "active_status": "Licensed product signal detected from FDA/CDC/openFDA sources",
            "regulatory_status": "FDA/CDC/openFDA text contained Zaire-specific indicated/approved/prevention language at the automated check.",
            "target_use": "Prevention of disease caused by Zaire ebolavirus in eligible populations and outbreak/pre-exposure programs according to applicable guidance.",
            "evidence_summary": "Automated regulatory parser found Zaire-specific ERVEBO indication/approval language in FDA, CDC, or openFDA label sources.",
            "next_milestone": "Continue automated monitoring for label, age indication, stockpile, prequalification, or guidance changes.",
            "development_geography": "United States regulatory/product sources; global outbreak-use relevance.",
            **make_source_fields([
                ("FDA ERVEBO product page", WATCH_PAGES["fda_ervebo"]),
                ("CDC Ebola vaccine product information", WATCH_PAGES["cdc_ervebo"]),
                ("openFDA drug label API", "https://open.fda.gov/apis/drug/label/"),
            ]),
            "notes": "Regulatory placement is generated from automated text/API checks, not manual curation.",
            "automation_method": "FDA/CDC/openFDA deterministic regulatory text check",
            "last_checked_utc": checked,
            "supporting_record_count": 3,
        })

    ema_zabdeno = pages.get("ema_zabdeno", "").lower()
    ema_mvabea = pages.get("ema_mvabea", "").lower()
    combined_ema = f"{ema_zabdeno} {ema_mvabea}"
    if "zabdeno" in combined_ema or "mvabea" in combined_ema or "ad26.zebov" in combined_ema or "mva-bn-filo" in combined_ema:
        withdrawn = any(term in combined_ema for term in ["withdrawn", "no longer authorised", "no longer authorized"])
        rows.append({
            "id": "zaire-zabdeno-mvabea-regulatory-auto",
            "target_species": "Zaire",
            "species_taxon": SPECIES_TAXON["Zaire"],
            "candidate": "Zabdeno + Mvabea / Ad26.ZEBOV + MVA-BN-Filo",
            "platform": "Heterologous prime-boost Ad26 vector + MVA vector regimen",
            "developer_sponsor": "Janssen / Johnson & Johnson; Bavarian Nordic",
            "stage_order": 6,
            "stage": STAGE_LABEL[6],
            "status_type": "Automated EMA regulatory signal",
            "active_status": "Formerly authorised / withdrawn signal detected" if withdrawn else "EMA product-page signal detected",
            "regulatory_status": "EMA page text contained withdrawn/no-longer-authorised language." if withdrawn else "EMA page text contained product information but automated parser did not detect withdrawal language.",
            "target_use": "Preventive Zaire ebolavirus vaccine pathway; operational availability depends on current regulatory/procurement status.",
            "evidence_summary": "Automated parser inspected EMA Zabdeno and Mvabea EPAR pages for current status language.",
            "next_milestone": "Monitor EMA and WHO procurement/prequalification sources for status and availability changes.",
            "development_geography": "European regulatory product pages; global procurement relevance.",
            **make_source_fields([
                ("EMA Zabdeno EPAR", WATCH_PAGES["ema_zabdeno"]),
                ("EMA Mvabea EPAR", WATCH_PAGES["ema_mvabea"]),
            ]),
            "notes": "Regulatory status is determined by text-match rules against EMA EPAR pages.",
            "automation_method": "EMA deterministic status text check",
            "last_checked_utc": checked,
            "supporting_record_count": 2,
        })

    who_sudan = pages.get("who_sudan_trial", "").lower()
    if all(term in who_sudan for term in ["sudan", "vaccine", "trial"]) and ("uganda" in who_sudan or "ring" in who_sudan):
        rows.append({
            "id": "sudan-iavi-rvsv-who-trial-auto",
            "target_species": "Sudan",
            "species_taxon": SPECIES_TAXON["Sudan"],
            "candidate": "IAVI rVSV-Sudan / Tokemeza SVD",
            "platform": "Replication-competent recombinant VSV vector expressing Sudan virus glycoprotein",
            "developer_sponsor": "IAVI; WHO-led outbreak trial partners",
            "stage_order": 5,
            "stage": STAGE_LABEL[5],
            "status_type": "Automated WHO outbreak-efficacy signal",
            "active_status": "Outbreak efficacy / ring-vaccination trial signal detected",
            "regulatory_status": "Investigational; no automated licensed-product signal detected for Sudan virus disease.",
            "target_use": "Rapid outbreak-response vaccination for Sudan virus disease contacts and contacts-of-contacts if trial/emergency pathway supports use.",
            "evidence_summary": "Automated parser found Sudan, vaccine, trial, and outbreak-trial context terms on the WHO Uganda trial page.",
            "next_milestone": "Monitor trial results, emergency-use decisions, manufacturing readiness, and regulatory submissions.",
            "development_geography": "Uganda / WHO-led outbreak trial context.",
            **make_source_fields([
                ("WHO Uganda Sudan vaccine trial page", WATCH_PAGES["who_sudan_trial"]),
            ]),
            "notes": "This rule promotes the Sudan rVSV candidate to outbreak efficacy/Phase 3-equivalent pathway when WHO trial language is detected.",
            "automation_method": "WHO deterministic outbreak-trial text check",
            "last_checked_utc": checked,
            "supporting_record_count": 1,
        })

    sabin = f"{pages.get('sabin_sudan', '')} {pages.get('sabin_phase2', '')}".lower()
    if "sabin" in sabin and "sudan" in sabin and "phase 2" in sabin:
        rows.append({
            "id": "sudan-sabin-cad3-phase2-auto",
            "target_species": "Sudan",
            "species_taxon": SPECIES_TAXON["Sudan"],
            "candidate": "Sabin cAd3-Sudan ebolavirus vaccine",
            "platform": "Chimpanzee adenovirus type 3 vector",
            "developer_sponsor": "Sabin Vaccine Institute",
            "stage_order": 4,
            "stage": STAGE_LABEL[4],
            "status_type": "Automated sponsor Phase 2 signal",
            "active_status": "Phase 2 signal detected from sponsor pages",
            "regulatory_status": "Investigational; no automated licensed-product signal detected for Sudan virus disease.",
            "target_use": "Prophylaxis for responders, healthcare workers, or at-risk groups subject to trial results and authorization.",
            "evidence_summary": "Automated parser found Sabin, Sudan, and Phase 2 language on sponsor program/update pages.",
            "next_milestone": "Monitor Phase 2 data readout, Phase 3 planning, emergency-use pathway, and manufacturing updates.",
            "development_geography": "Sponsor-reported clinical program context.",
            **make_source_fields([
                ("Sabin Sudan vaccine program", WATCH_PAGES["sabin_sudan"]),
                ("Sabin Phase 2 enrollment update", WATCH_PAGES["sabin_phase2"]),
            ]),
            "notes": "Sponsor pages are not used for licensure staging, only for Phase 2 pathway signal when explicit phase language is detected.",
            "automation_method": "Sponsor deterministic Phase 2 text check",
            "last_checked_utc": checked,
            "supporting_record_count": 2,
        })

    bdbv_text = f"{pages.get('who_bundibugyo', '')} {pages.get('cepi_bundibugyo', '')}".lower()
    if "bundibugyo" in bdbv_text and ("vaccine" in bdbv_text or "candidate" in bdbv_text):
        candidates = [
            (
                "bundibugyo-iavi-rvsv-preclinical-auto",
                "IAVI rVSV-Bundibugyo candidate",
                "Replication-competent recombinant VSV vector expressing Bundibugyo virus glycoprotein",
                "IAVI; CEPI/WHO outbreak-prioritization context",
                ["iavi", "rvsv"],
            ),
            (
                "bundibugyo-chadox1-preclinical-auto",
                "ChAdOx1 Bundibugyo candidate",
                "Replication-deficient ChAdOx1 adenoviral vector",
                "University of Oxford; Serum Institute of India; CEPI-supported context",
                ["chadox1", "oxford"],
            ),
            (
                "bundibugyo-moderna-mrna-preclinical-auto",
                "Moderna mRNA Bundibugyo candidate",
                "mRNA vaccine platform",
                "Moderna; CEPI-supported context",
                ["moderna", "mrna"],
            ),
        ]
        for row_id, candidate, platform, sponsor, must_have_any in candidates:
            if any(term in bdbv_text for term in must_have_any):
                rows.append({
                    "id": row_id,
                    "target_species": "Bundibugyo",
                    "species_taxon": SPECIES_TAXON["Bundibugyo"],
                    "candidate": candidate,
                    "platform": platform,
                    "developer_sponsor": sponsor,
                    "stage_order": 2,
                    "stage": STAGE_LABEL[2],
                    "status_type": "Automated WHO/CEPI clinical-enabling signal",
                    "active_status": "Preclinical / CMC / clinical-enabling signal detected",
                    "regulatory_status": "Investigational; no automated licensed-product signal detected for Bundibugyo virus disease.",
                    "target_use": "Preparedness or outbreak-response candidate for Bundibugyo virus disease, subject to clinical authorization and results.",
                    "evidence_summary": "Automated parser found Bundibugyo vaccine-candidate language on WHO/CEPI pages and candidate-specific sponsor/platform terms.",
                    "next_milestone": "Monitor animal-data package, clinical-grade manufacturing, Phase 1 authorization, and outbreak-trial decisions.",
                    "development_geography": "WHO/CEPI outbreak-prioritization context; candidate-specific development pathway.",
                    **make_source_fields([
                        ("WHO Bundibugyo vaccine prioritization page", WATCH_PAGES["who_bundibugyo"]),
                        ("CEPI Bundibugyo portfolio page", WATCH_PAGES["cepi_bundibugyo"]),
                    ]),
                    "notes": "Preclinical/clinical-enabling placement is deterministic and does not imply human efficacy or licensure.",
                    "automation_method": "WHO/CEPI deterministic candidate text check",
                    "last_checked_utc": checked,
                    "supporting_record_count": 2,
                })

    ebovac = pages.get("ebovac_platform", "").lower()
    if ("tai forest" in ebovac or "taï forest" in ebovac or "taï" in ebovac) and "mva" in ebovac:
        rows.append({
            "id": "taiforest-mva-bn-filo-antigen-auto",
            "target_species": "Taï Forest",
            "species_taxon": SPECIES_TAXON["Taï Forest"],
            "candidate": "MVA-BN-Filo Taï Forest antigen component",
            "platform": "Modified Vaccinia Ankara vector with multivalent filovirus inserts",
            "developer_sponsor": "Bavarian Nordic / Janssen regimen component",
            "stage_order": 1,
            "stage": STAGE_LABEL[1],
            "status_type": "Automated antigen-component signal",
            "active_status": "Antigen-component signal detected; no dedicated Taï Forest clinical pathway detected",
            "regulatory_status": "No automated Taï Forest disease indication detected.",
            "target_use": "No defined human vaccine-use pathway for Taï Forest virus disease from automated sources.",
            "evidence_summary": "Automated parser found Taï Forest/Tai Forest and MVA-BN-Filo platform terms in EBOVAC material, but no dedicated clinical-development or efficacy pathway signal.",
            "next_milestone": "Only relevant if risk assessment or dedicated Taï Forest virus vaccine-development activity changes.",
            "development_geography": "Historical multivalent antigen-component program context.",
            **make_source_fields([
                ("EBOVAC vaccine development page", WATCH_PAGES["ebovac_platform"]),
            ]),
            "notes": "Placed at discovery/antigen-component stage to avoid implying a validated Taï Forest vaccine indication.",
            "automation_method": "EBOVAC deterministic antigen-component text check",
            "last_checked_utc": checked,
            "supporting_record_count": 1,
        })

    return rows


def fetch_gdelt_discovery() -> list[dict[str, Any]]:
    # This is a discovery signal only. It is saved in raw reports but does not upstage
    # candidates, because news text is not a structured clinical or regulatory source.
    articles: list[dict[str, Any]] = []
    for query in GDELT_DISCOVERY_QUERIES:
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": "20",
            "sort": "DateDesc",
        }
        payload = fetch_json(GDELT_DISCOVERY_URL, params=params, label=f"GDELT discovery: {query}")
        if payload and isinstance(payload, dict):
            for article in payload.get("articles", []) or []:
                article["dashboard_query"] = query
                articles.append(article)
        time.sleep(0.3)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / "gdelt_discovery_articles.json").write_text(json.dumps(articles[:100], indent=2), encoding="utf-8")
    return articles


def gap_rows(existing_rows: dict[str, dict[str, Any]], pages: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    checked = now_utc_iso()
    cdc_text = pages.get("cdc_basics", "")
    for species, taxon in SPECIES_TAXON.items():
        has_species_signal = any(r.get("target_species") == species and int(r.get("stage_order", 0)) > 0 for r in existing_rows.values())
        if has_species_signal:
            continue
        source_label = "CDC Ebola disease basics" if cdc_text else "Automated source set"
        source_url = WATCH_PAGES["cdc_basics"] if cdc_text else ""
        rows.append({
            "id": f"{slugify(species)}-automated-gap",
            "target_species": species,
            "species_taxon": taxon,
            "candidate": f"Dedicated {species} virus human vaccine pathway",
            "platform": "No dedicated human vaccine pathway detected by automated sources",
            "developer_sponsor": "None detected by automated parser",
            "stage_order": 0,
            "stage": STAGE_LABEL[0],
            "status_type": "Automated gap lane",
            "active_status": "No clinical, regulatory, or configured preclinical signal detected",
            "regulatory_status": f"No licensed human vaccine signal detected for {species} virus disease.",
            "target_use": "Not defined from automated sources.",
            "evidence_summary": f"The weekly automation did not detect a dedicated {species} vaccine pathway in ClinicalTrials.gov, regulator pages/APIs, or configured WHO/CEPI/sponsor watch pages.",
            "next_milestone": "Monitor automated sources for new trial registrations, regulator pages, WHO/CEPI announcements, or structured discovery signals.",
            "development_geography": "Not applicable.",
            **make_source_fields([(source_label, source_url)] if source_url else []),
            "notes": "Gap rows are generated automatically when no stronger signal is detected.",
            "automation_method": "Automated absence-of-signal rule across configured sources",
            "last_checked_utc": checked,
            "supporting_record_count": 0,
        })
    return rows


def write_csv(rows: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sorted_rows = sorted(rows, key=lambda r: (r.get("target_species", ""), -int(r.get("stage_order", 0)), r.get("candidate", "")))
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REQUIRED_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted_rows:
            clean_row = {col: row.get(col, "") for col in REQUIRED_COLUMNS}
            writer.writerow(clean_row)


def write_reports(rows: list[dict[str, Any]], gdelt_articles: list[dict[str, Any]]) -> None:
    REPORTS.mkdir(exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / "source_fetch_status.json").write_text(json.dumps(source_status, indent=2, sort_keys=True), encoding="utf-8")
    counts_by_species = Counter(row.get("target_species", "") for row in rows)
    max_stage_by_species: dict[str, int] = defaultdict(int)
    for row in rows:
        sp = str(row.get("target_species", ""))
        max_stage_by_species[sp] = max(max_stage_by_species[sp], int(row.get("stage_order", 0)))

    lines = [
        "# Ebola vaccine pipeline automation summary",
        "",
        f"Run completed: {now_utc_iso()}",
        f"Generated rows: {len(rows)}",
        f"Warnings: {len(warnings)}",
        f"GDELT discovery articles saved: {len(gdelt_articles)}",
        "",
        "## Rows by species",
        "",
    ]
    for species in sorted(SPECIES_TAXON):
        lines.append(f"- {species}: {counts_by_species.get(species, 0)} row(s); max stage order {max_stage_by_species.get(species, 0)}")
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {w}" for w in warnings)
    lines.extend([
        "",
        "## Automation method",
        "",
        "1. Pull ClinicalTrials.gov v2 records using Ebola vaccine queries, deduplicate by NCT ID, and aggregate by candidate/species.",
        "2. Use trial phase fields for clinical-stage placement.",
        "3. Use FDA/CDC/openFDA and EMA product pages/APIs for Zaire regulatory-stage placement.",
        "4. Use configured WHO/CEPI/sponsor watch pages for explicit Sudan outbreak-trial, Sudan Phase 2, Bundibugyo clinical-enabling, and Taï Forest antigen-component signals.",
        "5. Generate gap lanes for species with no detected dedicated signal.",
        "6. Save news-discovery articles from GDELT as audit material only; news does not upstage a candidate.",
    ])
    AUTOMATION_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    studies = fetch_clinicaltrials()
    aggregates = aggregate_clinical_rows(studies)
    rows_by_id: dict[str, dict[str, Any]] = {}
    for agg in aggregates.values():
        add_or_upgrade(rows_by_id, row_from_aggregate(agg))

    pages = fetch_watch_pages()
    openfda_payload = fetch_openfda_ervebo()
    for row in regulatory_and_watch_rows(pages, openfda_payload):
        add_or_upgrade(rows_by_id, row)

    gdelt_articles = fetch_gdelt_discovery()
    for row in gap_rows(rows_by_id, pages):
        add_or_upgrade(rows_by_id, row)

    rows = list(rows_by_id.values())
    if not rows:
        raise RuntimeError("Automation produced no rows; refusing to overwrite data/pipeline.csv")
    write_csv(rows)
    write_reports(rows, gdelt_articles)
    print(f"Generated {len(rows)} pipeline rows into {OUT_CSV.relative_to(ROOT)}")
    if warnings:
        print(f"Completed with {len(warnings)} warning(s). See reports/automation_summary.md", file=sys.stderr)


if __name__ == "__main__":
    main()
