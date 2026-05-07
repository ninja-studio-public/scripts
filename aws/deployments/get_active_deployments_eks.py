# Script Version: 1.3.0 dated 07 May 2026

# This script queries Docker image tags currently running per microservice,
# across one or more named environments (e.g. staging, prod), each mapped
# to an EKS cluster. Output is written to $OUTPUT_DIR/active-deployments.json.
#
# Arguments:
#   argv[1]  region        eg. "ca-central-1"
#   argv[2]  microservices eg. "backend,frontend,caddy"  (comma-separated)
#   argv[3+] environments  eg. "staging:myproject-staging" "prod:myproject-production"
#
# Service names are derived from pod names by stripping the last two segments
# (replicaset hash + pod hash), e.g. "myproject-backend-7d9f8c-xkp2q" -> "myproject-backend".
# The microservices argument should use these derived names exactly.
#
# Pods are queried from the "default" namespace only.
# kubectl must be installed and available on PATH.
# The script calls `aws eks update-kubeconfig` per cluster — no pre-configuration needed.
#
# Authentication:
#   The script assumes AWS credentials are already present in the environment
#   before it is called. No credentials are accepted as arguments.
#   Supported credential sources (in order of typical usage):
#     - GitHub Actions:  provided by the configure-aws-credentials OIDC step
#     - Local CLI:       ~/.aws/credentials or an active `aws sso login` session
#     - Docker:          passed to the container via --env-file or -e flags at runtime
#     - Cloud Shell:     session is pre-authenticated by the cloud provider
#
#   If the target EKS clusters require an IAM role to be assumed on top of the
#   ambient credentials (e.g. cross-account access), set ROLE_ARN. The script
#   will call `aws sts assume-role` and pass the resulting credentials explicitly
#   to each subprocess — os.environ is never mutated.
#
#   OPTIONAL Environment variables:
#   ROLE_ARN    — IAM role to assume before querying clusters (optional)
#                 eg. "arn:aws:iam::123456789012:role/MyRole"
#   OUTPUT_DIR  — directory for active-deployments.json (optional)
#                 Defaults to "./output" if not set.
#                 Set to "/app/output" for Docker, or
#                 "${{ github.workspace }}/output" in GitHub Actions.
#
# Example (local / Cloud Shell):
#   export ROLE_ARN=arn:aws:iam::123456789012:role/MyRole   # optional
#   export OUTPUT_DIR=./output                              # optional
#   python3 get_active_deployments_eks.py \
#          "ca-central-1" \
#          "backend,frontend,caddy" \
#          "staging:myproject-staging" "prod:myproject-production"
#

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from sys import argv
from typing import Optional

# ── Inputs ────────────────────────────────────────────────────────────────────
region_name   = argv[1]
microservices = [x.strip() for x in argv[2].split(',')]
environments  = {}
for arg in argv[3:]:
    name, cluster_name = arg.split(':', 1)
    environments[name.strip()] = cluster_name.strip()

OUTPUT_DIR  = os.environ.get("OUTPUT_DIR", "./output")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "active-deployments.json")
ROLE_ARN    = os.environ.get("ROLE_ARN", "")

# ── Colors ────────────────────────────────────────────────────────────────────

class Colors:
    BLUE      = '\033[94m'
    CYAN      = '\033[96m'
    GREEN     = '\033[92m'
    YELLOW    = '\033[93m'
    RED       = '\033[91m'
    BOLD      = '\033[1m'
    UNDERLINE = '\033[4m'
    END       = '\033[0m'

# ── Helpers ───────────────────────────────────────────────────────────────────

def assume_role(role_arn: str, region: str) -> Optional[dict]:
    """Assume an IAM role via STS and return the credentials as a dict.

    Credentials are returned explicitly and passed to subprocesses via their
    env parameter — os.environ is never mutated.
    Returns a dict with AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
    AWS_SESSION_TOKEN on success, or None on failure.
    """
    command = [
        "aws", "sts", "assume-role",
        "--role-arn", role_arn,
        "--role-session-name", "eks-active-deployments",
        "--region", region,
        "--query", "Credentials.[AccessKeyId,SecretAccessKey,SessionToken]",
        "--output", "text"
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        key_id, secret_key, session_token = result.stdout.strip().split("\t")
        return {
            "AWS_ACCESS_KEY_ID":     key_id,
            "AWS_SECRET_ACCESS_KEY": secret_key,
            "AWS_SESSION_TOKEN":     session_token,
        }
    except subprocess.CalledProcessError as e:
        print(f"{Colors.RED}  [ERROR] Could not assume role {role_arn}{Colors.END}")
        print(f"{Colors.RED}  [ERROR] exit code: {e.returncode}{Colors.END}")
        print(f"{Colors.RED}  [ERROR] stderr: {e.stderr.strip()}{Colors.END}")
        return None
    except Exception as e:
        print(f"{Colors.RED}  [ERROR] Unexpected error assuming role {role_arn}: {e}{Colors.END}")
        return None        

def configure_kubeconfig(cluster_name: str, region: str, aws_env: Optional[dict] = None) -> bool:
    """Run aws eks update-kubeconfig for the given cluster.

    If aws_env is provided, it is merged into the subprocess environment so
    that assumed-role credentials are used without mutating os.environ.
    Returns True on success, False on failure.
    """
    command = [
        "aws", "eks", "update-kubeconfig",
        "--name", cluster_name,
        "--region", region
    ]
    env = {**os.environ, **(aws_env or {})}
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, env=env)
        return True
    except subprocess.CalledProcessError as e:
        print(f"{Colors.RED}  [ERROR] Could not configure kubeconfig for {cluster_name}: {e.stderr}{Colors.END}")
        return False


def get_pods_json(aws_env: Optional[dict] = None) -> Optional[dict]:
    """Run kubectl get pods -o json against the default namespace.

    aws_env, if provided, is merged into the subprocess environment so that
    assumed-role credentials are used without mutating os.environ.
    Returns parsed JSON dict, or None on failure.
    """
    command = ["kubectl", "get", "pods", "-n", "default", "-o", "json"]
    env     = {**os.environ, **(aws_env or {})}
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, env=env)
        return json.loads(result.stdout.strip())
    except subprocess.CalledProcessError as e:
        print(f"{Colors.RED}  [ERROR] kubectl failed: {e.stderr}{Colors.END}")
        return None
    except json.JSONDecodeError as e:
        print(f"{Colors.RED}  [ERROR] Failed to parse kubectl output: {e}{Colors.END}")
        return None


def derive_service_name(pod_name: str) -> str:
    """Derive the service name from a pod name by stripping the last two
    hyphen-separated segments (replicaset hash + pod hash).

    e.g. "myproject-backend-7d9f8c-xkp2q"   -> "myproject-backend"
         "caddy-gateway-6b4d9f-abc12"  -> "caddy-gateway"
    """
    parts = pod_name.split("-")
    return "-".join(parts[:-2]) if len(parts) > 2 else pod_name


def get_cluster_images(cluster_name: str, region: str, aws_env: Optional[dict] = None) -> dict:
    """Return a mapping of {service_name: image_tag} for all pods in the cluster.

    Configures kubeconfig, queries pods in the default namespace, and derives
    service names using the same logic as the kimages bash function.
    One kubectl call per cluster — all microservice lookups share the result.
    aws_env, if provided, is passed to all subprocesses so that assumed-role
    credentials are used without mutating os.environ.
    """
    if not configure_kubeconfig(cluster_name, region, aws_env):
        return {}

    data = get_pods_json(aws_env)
    if not data:
        return {}

    images = {}
    for item in data.get("items", []):
        pod_name   = item.get("metadata", {}).get("name", "")
        service    = derive_service_name(pod_name)
        containers = item.get("spec", {}).get("containers", [])
        for container in containers:
            image = container.get("image", "")
            if not image:
                continue
            match          = re.search(r":(.+)$", image)
            images[service] = match.group(1) if match else image  # last pod wins; replicas share the same image
    return images


def build_regex_pattern(tag_list: list) -> str:
    r"""Convert a list of tags to a combined regex pattern.

    e.g. ['v1.1.0', 'v1.2.0'] -> '(?:v1\.1\.0)|(?:v1\.2\.0)'
    """
    if not tag_list:
        return ''
    return '(?:' + ')|(?:'.join(re.escape(tag) for tag in tag_list) + ')'


def write_output(results: dict, filepath: str) -> None:
    """Write active deployments to a JSON file with plain tags and regex patterns.

    Structure: { microservice: { env: tag, ..., "pattern": "..." } }
    """
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    output = {}
    for svc, env_tags in results.items():
        valid_tags = sorted(set(t for t in env_tags.values() if t != "invalid"))
        output[svc] = {**env_tags, "pattern": build_regex_pattern(valid_tags)}
    with open(filepath, "w") as f:
        json.dump(output, f, indent=2)


def print_box_header(title: str, subtitle: str = "", datetime_str: str = "") -> None:
    """Print a boxed header."""
    width       = 74
    left_indent = " " * 2
    print(f"\n{Colors.BLUE}{left_indent}╔{'═' * (width - 2)}╗{Colors.END}")

    title_padding = (width - len(title) - 2) // 2
    print(f"{Colors.BLUE}{left_indent}║{' ' * title_padding}{Colors.END}{Colors.BOLD}{title}{Colors.END}{Colors.BLUE}{' ' * (width - len(title) - title_padding - 2)}║{Colors.END}")

    if subtitle:
        subtitle_padding = (width - len(subtitle) - 2) // 2
        print(f"{Colors.BLUE}{left_indent}║{' ' * subtitle_padding}{Colors.END}{subtitle}{Colors.BLUE}{' ' * (width - len(subtitle) - subtitle_padding - 2)}║{Colors.END}")

    if datetime_str:
        dt_padding = (width - len(datetime_str) - 2) // 2
        print(f"{Colors.BLUE}{left_indent}║{' ' * dt_padding}{Colors.END}{Colors.CYAN}{datetime_str}{Colors.END}{Colors.BLUE}{' ' * (width - len(datetime_str) - dt_padding - 2)}║{Colors.END}")

    print(f"{Colors.BLUE}{left_indent}╚{'═' * (width - 2)}╝{Colors.END}\n")


def wrap(text: str, width: int) -> list:
    """Split text into lines of at most `width` characters."""
    return [text[i:i + width] for i in range(0, max(len(text), 1), width)]


def tag_color(tag: str) -> str:
    """Choose a display color based on the tag value."""
    if tag == "(none)":
        return Colors.RED
    if re.match(r"^v?\d+\.\d+\.\d+$", tag):
        return Colors.GREEN
    if re.match(r"^[0-9a-f]{12}$", tag):    # raw image ID
        return Colors.CYAN
    return Colors.CYAN                       # non-standard tags (hotfix branches, 'latest', etc.)


def print_summary(results: dict, environments: dict) -> None:
    """Print a wrapped, coloured summary table showing each microservice's tag per environment."""
    MAX_TAG_COL = 24

    env_names = list(environments.keys())
    col_svc   = max(len("Microservice"), max(len(s) for s in results)) + 2
    col_tag   = max(max(len(e) for e in env_names) + 2, MAX_TAG_COL)

    C         = Colors
    div_plain = "+" + "-" * (col_svc + 2) + "+" + "".join("-" * (col_tag + 2) + "+" for _ in env_names)
    divider   = f"  {C.BLUE}{div_plain}{C.END}"
    header    = f"  {C.BLUE}|{C.END} {C.BOLD}{'Microservice':<{col_svc}}{C.END} {C.BLUE}|{C.END}" + \
                "".join(f" {C.BOLD}{e:<{col_tag}}{C.END} {C.BLUE}|{C.END}" for e in env_names)

    print(divider)
    print(header)
    print(divider)

    for svc, env_tags in results.items():
        wrapped = {}
        for env in env_names:
            tag = env_tags.get(env, "(none)")
            if tag == "invalid":
                tag = "(none)"
            wrapped[env] = wrap(tag, col_tag)

        row_count = max(len(lines) for lines in wrapped.values())
        for i in range(row_count):
            svc_cell = svc if i == 0 else ""
            row      = f"  {C.BLUE}|{C.END} {C.BOLD}{svc_cell:<{col_svc}}{C.END} {C.BLUE}|{C.END}"
            for env in env_names:
                cell  = wrapped[env][i] if i < len(wrapped[env]) else ""
                color = tag_color(cell) if i == 0 else tag_color(wrapped[env][0])
                row  += f" {color}{cell}{C.END}{' ' * (col_tag - len(cell))} {C.BLUE}|{C.END}"
            print(row)
        print(divider)
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

now      = datetime.now(timezone.utc).strftime("%Y-%m-%d  %H:%M:%S UTC")
env_list = ", ".join(environments.keys())

print_box_header(
    title        = "Active Deployments (EKS)",
    subtitle     = f"Region: {region_name}   |   Environments: {env_list}",
    datetime_str = now
)

if ROLE_ARN:
    print(f"  {Colors.CYAN}Assuming role: {ROLE_ARN}{Colors.END}")
    aws_env = assume_role(ROLE_ARN, region_name)
    if not aws_env:
        raise SystemExit(1)
else:
    aws_env = None

# Fetch all pod images per cluster upfront — one kubectl call per cluster.
cluster_images: dict = {}
for env, cluster_name in environments.items():
    print(f"  {Colors.CYAN}Querying cluster: {cluster_name}{Colors.END}")
    cluster_images[env] = get_cluster_images(cluster_name, region_name, aws_env)

# results[microservice][env] = tag
results = {svc: {} for svc in microservices}

for svc in microservices:
    for env in environments:
        results[svc][env] = cluster_images[env].get(svc, "invalid")

print_summary(results, environments)

write_output(results, OUTPUT_FILE)
print(f"  {Colors.GREEN}Output written to active-deployments.json{Colors.END}\n")
