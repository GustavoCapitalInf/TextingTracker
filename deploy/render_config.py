"""Render deployment templates without installing or starting any services."""

import argparse
import ipaddress
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", required=True, help="Actual internal DNS name")
    parser.add_argument("--lan-ip", required=True, help="Reserved private IPv4 address")
    parser.add_argument("--lan-cidr", required=True, help="Allowed private IPv4 office subnet")
    parser.add_argument("--brew-prefix", choices=("/opt/homebrew", "/usr/local"), required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "generated")
    args = parser.parse_args()
    hostname = args.hostname.lower().rstrip(".")
    if len(hostname) > 253 or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", hostname
    ):
        parser.error("hostname must be a DNS name with at least two labels")
    try:
        address = ipaddress.IPv4Address(args.lan_ip)
        network = ipaddress.IPv4Network(args.lan_cidr, strict=True)
    except ValueError as exc:
        parser.error(str(exc))
    office_ranges = [ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    if not any(network.subnet_of(block) for block in office_ranges):
        parser.error("LAN CIDR must be within an RFC 1918 private IPv4 range")
    if address not in network or address in (network.network_address, network.broadcast_address):
        parser.error("LAN address must be a usable host in the allowed office subnet")
    replacements = {
        "@@HOSTNAME@@": hostname,
        "@@HOST_REGEX@@": re.escape(hostname),
        "@@LAN_IP@@": str(address),
        "@@LAN_CIDR@@": str(network),
        "@@BREW_PREFIX@@": args.brew_prefix,
    }
    templates = sorted((Path(__file__).parent / "templates").glob("*.in"))
    targets = [args.output / template.name.removesuffix(".in") for template in templates]
    if any(target.exists() for target in targets):
        parser.error("output contains generated files already; choose a new --output directory")
    args.output.mkdir(parents=True, exist_ok=True)
    for template, target in zip(templates, targets):
        content = template.read_text(encoding="utf-8")
        for key, value in replacements.items():
            content = content.replace(key, value)
        if "@@" in content:
            raise ValueError(f"Unresolved placeholder in {template.name}")
        target.write_text(content, encoding="utf-8", newline="\n")
    print(f"Wrote {len(targets)} configuration files to {args.output.resolve()}.")
    print("Nothing was installed or started. Follow docs/mac-mini-deployment.md.")


if __name__ == "__main__":
    main()

