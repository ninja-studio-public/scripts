# This script queries Docker image tags currently running per microservice,
# across one or more named environments (e.g. staging, prod), each mapped
# to an EC2 instance. Output is written to /app/output/active-deployments.json.
#
# Arguments:
#   argv[1]  region        eg. "eu-west-1"
#   argv[2]  microservices eg. "frontend,backend,caddy,tester"  (comma-separated)
#   argv[3+] environments  eg. "staging:i-1234567890abcdef0" "prod:i-0987654321fedcba0"
#
# Requires AWS credentials with SSM privileges, supplied via environment variables:
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, (optionally) AWS_SESSION_TOKEN
# OR modify line ~55 to pass credentials explicitly to the boto3 client.
#
# Recommended: run via Docker using the provided run-active-deployments.sh script.
# To run locally with a venv:
#   python -m venv .venv && source .venv/bin/activate
#   pip install boto3
#   REF: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/quickstart.html
#
# Script Version: 1.5.0 dated 23 March 2026

import json
import os
import re
import boto3
from datetime import datetime, timezone
from time import sleep
from sys import argv

# ── Inputs ────────────────────────────────────────────────────────────────────
region_name   = argv[1]                                   # eg. "eu-west-1"
microservices = [x.strip() for x in argv[2].split(',')]   # eg. "frontend,backend"
environments  = {}                                        # eg. {"staging": "i-abc", "prod": "i-def"}
for arg in argv[3:]:
    name, instance_id = arg.split(':', 1)
    environments[name.strip()] = instance_id.strip()

OUTPUT_DIR  = os.environ.get("OUTPUT_DIR", "/app/output")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "active-deployments.json")
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

def get_image_tag(instance_id: str, microservice: str, region_name: str) -> str:
    """Query the running Docker image tag for a given microservice on an EC2 instance
    via AWS SSM, without requiring direct SSH access.

    Returns the full tag string (e.g. 'v1.48.0'), or 'invalid' on any failure.

    Prerequisites:
      - SSM Agent installed and running on the EC2 instance.
      - Instance IAM role permits ssm:SendCommand and ssm:GetCommandInvocation.
    """
    ssm_client = boto3.client('ssm', region_name=region_name)
    command = f"docker ps --filter name={microservice} --format '{{{{.Image}}}}'"

    try:
        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': [command]}
        )
        command_id = response['Command']['CommandId']
    except Exception as e:
        print(f"{Colors.RED}  [ERROR] Could not send SSM command to {instance_id}: {e}{Colors.END}")
        return "invalid"

    # Poll for result (up to 50 s)
    for _ in range(10):
        sleep(5)
        output = ssm_client.get_command_invocation(
            CommandId=command_id,
            InstanceId=instance_id
        )
        if output['Status'] in ['Success', 'Failed', 'TimedOut', 'Cancelled']:
            break

    if output['Status'] != 'Success':
        raise Exception(f"SSM command failed: {output['Status']} — {output.get('StandardErrorContent')}")

    image = output['StandardOutputContent'].strip()
    if not image:
        return "invalid"

    match = re.search(r":(.+)$", image)
    return match.group(1) if match else image


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
    width = 74
    left_indent=" "*2
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
    return [text[i:i+width] for i in range(0, max(len(text), 1), width)]


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

    C = Colors
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
            svc_cell  = svc if i == 0 else ""
            row = f"  {C.BLUE}|{C.END} {C.BOLD}{svc_cell:<{col_svc}}{C.END} {C.BLUE}|{C.END}"
            for env in env_names:
                cell  = wrapped[env][i] if i < len(wrapped[env]) else ""
                color = tag_color(cell) if i == 0 else tag_color(wrapped[env][0])
                # pad without color codes interfering with width
                row  += f" {color}{cell}{C.END}{' ' * (col_tag - len(cell))} {C.BLUE}|{C.END}"
            print(row)
        print(divider)
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

now = datetime.now(timezone.utc).strftime("%Y-%m-%d  %H:%M:%S UTC")
env_list = ", ".join(environments.keys())

print_box_header(
    title       = "Active Deployments",
    subtitle    = f"Region: {region_name}   |   Environments: {env_list}",
    datetime_str= now
)

# results[microservice][env] = tag
results = {svc: {} for svc in microservices}

for svc in microservices:
    for env, instance_id in environments.items():
        tag = get_image_tag(instance_id, svc, region_name)
        results[svc][env] = tag

print_summary(results, environments)

write_output(results, OUTPUT_FILE)
print(f"  {Colors.GREEN}Output written to active-deployments.json{Colors.END}\n")
