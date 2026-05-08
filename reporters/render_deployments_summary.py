# This script reads active-deployments.json and writes an HTML deployment
# summary to $GITHUB_STEP_SUMMARY for display in the GitHub Actions Summary tab.
#
# Arguments:
#   argv[1+] environments  eg. "staging:myproject-staging" "production:myproject-production"
#                          For EC2, values are instance IDs; for EKS, cluster names.
#                          These are used for display purposes only.
#                          Environment names must match the keys in active-deployments.json.
#
# Environment variables:
#   INPUT_FILE          — path to active-deployments.json
#                         Defaults to "./output/active-deployments.json"
#   GITHUB_STEP_SUMMARY — path to the GitHub step summary file (set automatically
#                         by GitHub Actions). If not set, output is written to stdout.
#
# Example (local / Cloud Shell):
#   python3 render_deployments_summary.py \
#     "staging:myproject-staging" "prod:myproject-production"
#
# Example (GitHub Actions):
#   python3 scripts/reporters/render_deployments_summary.py \
#     ${{ inputs.environments }}
#
# Script Version: 1.3.0 dated 08 May 2026

import json
import os
import re
import sys
from datetime import datetime, timezone
from sys import argv

# ── Inputs ────────────────────────────────────────────────────────────────────
environments = {}
for arg in argv[1:]:
    name, target = arg.split(':', 1)
    environments[name.strip()] = target.strip()

INPUT_FILE   = os.environ.get("INPUT_FILE", "./output/active-deployments.json")
SUMMARY_FILE = os.environ.get("GITHUB_STEP_SUMMARY", None)

# ── Load data ─────────────────────────────────────────────────────────────────
try:
    with open(INPUT_FILE, "r") as f:
        data = json.load(f)
except FileNotFoundError:
    print(f"[ERROR] Input file not found: {INPUT_FILE}", file=sys.stderr)
    raise SystemExit(1)
except json.JSONDecodeError as e:
    print(f"[ERROR] Failed to parse {INPUT_FILE}: {e}", file=sys.stderr)
    raise SystemExit(1)

# Derive environment names from the JSON keys of the first service entry,
# excluding the 'pattern' key. This ensures the renderer is always in sync
# with the collection script regardless of what names were passed as arguments.
first_svc = next(iter(data.values()))
env_names = [k for k in first_svc.keys() if k != "pattern"]

# ── Colors ────────────────────────────────────────────────────────────────────
COLOR_GREEN  = "#4caf50"   # valid semver tags  eg. v1.1385.0
COLOR_CYAN   = "#00bcd4"   # non-standard tags  eg. latest, branch names
COLOR_RED    = "#f44336"   # missing / invalid

# ── Helpers ───────────────────────────────────────────────────────────────────

def tag_color(tag: str) -> str:
    """Return a hex color based on the tag value, mirroring the terminal script logic."""
    if not tag or tag in ("invalid", "(none)"):
        return COLOR_RED
    if re.match(r"^v?\d+\.\d+\.\d+$", tag):
        return COLOR_GREEN
    return COLOR_CYAN


def clean_tag(tag: str) -> str:
    """Return a display-safe tag value, replacing invalid markers with a dash."""
    if not tag or tag in ("invalid", "(none)"):
        return "-"
    return tag


def colored_tag_cell(tag: str) -> str:
    """Render a table cell with the tag value colored and padded."""
    display = clean_tag(tag)
    color   = tag_color(tag)
    return f'<td style="padding:6px 12px;"><code style="color:{color};">{display}</code></td>'


def find_diffs(data: dict, env_names: list) -> list:
    """Return a list of (microservice, {env: tag}) for services where tags differ
    across environments, or where at least one environment is missing/invalid
    while another has a valid tag.
    """
    diffs = []
    for svc, env_tags in data.items():
        tags        = {env: env_tags.get(env, "invalid") for env in env_names}
        valid_tags  = {t for t in tags.values() if t not in ("invalid", "(none)")}
        has_invalid = any(t in ("invalid", "(none)") for t in tags.values())
        if len(valid_tags) > 1 or (has_invalid and len(valid_tags) >= 1):
            diffs.append((svc, tags))
    return diffs


def resolve_cluster(env_name: str, environments: dict) -> str:
    """Resolve a cluster/instance name for a given env_name.

    Tries exact match first, then falls back to finding an argv key that
    is a prefix of the env_name (e.g. "prod" matches "production").
    Returns "n/a" if no match found.
    """
    if env_name in environments:
        return environments[env_name]
    for key, value in environments.items():
        if env_name.startswith(key) or key.startswith(env_name):
            return value
    return "n/a"


def render_html(data: dict, environments: dict, env_names: list, timestamp: str) -> str:
    """Render the full HTML summary."""
    diffs = find_diffs(data, env_names)

    th_style      = 'style="padding:6px 12px; text-align:left;"'
    td_svc_style  = 'style="padding:6px 12px; font-weight:bold;"'

    cluster_rows = "".join(
        f'<tr>'
        f'<td style="padding:6px 12px;"><strong>{env}</strong></td>'
        f'<td style="padding:6px 12px;"><code>{resolve_cluster(env, environments)}</code></td>'
        f'</tr>'
        for env in env_names
    )

    env_headers  = "".join(f"<th {th_style}>{env}</th>" for env in env_names)

    service_rows = ""
    for svc, env_tags in data.items():
        cells        = "".join(colored_tag_cell(env_tags.get(env, "invalid")) for env in env_names)
        service_rows += f"<tr><td {td_svc_style}>{svc}</td>{cells}</tr>\n"

    if diffs:
        diff_rows = ""
        for svc, tags in diffs:
            cells     = "".join(colored_tag_cell(tags.get(env, "invalid")) for env in env_names)
            diff_rows += f"<tr><td {td_svc_style}>{svc}</td>{cells}</tr>\n"
        diff_section = f"""
<h2>Version Mismatches</h2>
<table>
  <thead>
    <tr><th {th_style}>Service</th>{env_headers}</tr>
  </thead>
  <tbody>
    {diff_rows}
  </tbody>
</table>
"""
    else:
        diff_section = "<p> All services are on the same version across environments.</p>"

    return f"""
<h1>Active Deployments</h1>
<table>
  <thead>
    <tr><th {th_style}>Environment</th><th {th_style}>Cluster / Instance</th></tr>
  </thead>
  <tbody>
    {cluster_rows}
  </tbody>
</table>
<p><em>Generated: {timestamp}</em></p>

<h2>Deployed Versions</h2>
<table>
  <thead>
    <tr><th {th_style}>Service</th>{env_headers}</tr>
  </thead>
  <tbody>
    {service_rows}
  </tbody>
</table>

{diff_section}
"""

# ── Main ──────────────────────────────────────────────────────────────────────

timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
html      = render_html(data, environments, env_names, timestamp)

if SUMMARY_FILE:
    with open(SUMMARY_FILE, "a") as f:
        f.write(html)
    print(f"Summary written to {SUMMARY_FILE}")
else:
    print(html)