"""Install egress rules only in this container's own network namespace.

There is no host networking, Docker socket, host mount, or host firewall mutation.
The daemon has no capabilities after initialization. Only the separate watchdog
UID may call the loopback RPC port, so a download URI cannot use that exception.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import subprocess

DENY_V4 = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)
DENY_V6 = ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20")


def blocked_networks(value: str) -> list:
    return [ipaddress.ip_network(part.strip(), strict=False)
            for part in value.split(",") if part.strip()]


def nameservers(text: str) -> list:
    result = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            result.append(ipaddress.ip_address(fields[1]))
    if not result:
        raise RuntimeError("no explicit container DNS resolver is configured")
    return result


def rules(resolvers: list, additional: list) -> str:
    v4 = list(ipaddress.collapse_addresses([ipaddress.ip_network(net) for net in DENY_V4]
                                           + [net for net in additional if net.version == 4]))
    v6 = list(ipaddress.collapse_addresses([ipaddress.ip_network(net) for net in DENY_V6]
                                           + [net for net in additional if net.version == 6]))
    lines = ["table inet ft_offline {",
             " set site_ready { typeof meta skuid; flags timeout; timeout 15s; }",
             " set site_v4 { type ipv4_addr; }",
             " set site_v6 { type ipv6_addr; }",
             " chain site_guard {",
             "  meta skuid != @site_ready reject",
             "  ip daddr @site_v4 reject",
             "  ip6 daddr @site_v6 reject",
             " }", " chain output {",
             "  type filter hook output priority 0; policy accept;",
             "  meta skuid 10001 ct direction original jump site_guard",
             "  ct state established,related accept",
             "  meta skuid 10002 ip daddr 127.0.0.1 tcp dport 6800 accept"]
    # Docker's embedded resolver DNATs port 53 in this namespace. Match the
    # original conntrack port instead of allowing every port on the resolver.
    for resolver in resolvers:
        family = "ip" if resolver.version == 4 else "ip6"
        for protocol in ("udp", "tcp"):
            lines.append(f"  {family} daddr {resolver} meta l4proto {protocol} ct original proto-dst 53 accept")
    lines.extend([f"  ip daddr {{ {', '.join(map(str, v4))} }} reject",
                  "  ip6 daddr != 2000::/3 reject",
                  f"  ip6 daddr {{ {', '.join(map(str, v6))} }} reject",
                  " }", "}"])
    return "\n".join(lines) + "\n"


def install() -> None:
    if os.environ.get("FT_ARIA2_NETWORK_MODE", "bridge") != "bridge":
        raise RuntimeError("only an isolated bridge network is supported")
    additional = blocked_networks(os.environ.get("FT_ARIA2_BLOCKED_NETWORKS", ""))
    data = rules(nameservers(Path("/etc/resolv.conf").read_text()), additional)
    # nft is not optional: failure prevents the downloader process from starting.
    subprocess.run(["nft", "-f", "-"], input=data, text=True, check=True,
                   stdout=subprocess.DEVNULL)
