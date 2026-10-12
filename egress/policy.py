"""Canonical exact domains and public-address-only resolution. No wildcard grants."""

import asyncio
import ipaddress
import re
import socket


def domain(value):
    if not isinstance(value, str) or len(value) > 253 or value != value.lower():
        raise ValueError("canonical_domain_required")
    labels = value.split(".")
    if len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in labels
    ):
        raise ValueError("invalid_domain")
    # ASCII/punycode only; excludes libc legacy numeric-address spellings.
    if not re.search(r"[a-z]", labels[-1]) or labels[-1] in {
        "localhost",
        "local",
        "internal",
        "test",
        "invalid",
        "example",
        "onion",
    }:
        raise ValueError("non_public_domain")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("literal_ip_forbidden")


def public_ip(value):
    ip = ipaddress.ip_address(value)
    # IPv6 transition mechanisms can wrap an otherwise forbidden destination.
    if (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_loopback
        or ip.is_link_local
    ):
        raise ValueError("non_public_address")
    if isinstance(ip, ipaddress.IPv6Address) and (
        ip not in ipaddress.ip_network("2000::/3")
        or ip.ipv4_mapped
        or ip.sixtofour
        or ip.teredo
        or ip in ipaddress.ip_network("64:ff9b::/96")
        or ip in ipaddress.ip_network("64:ff9b:1::/48")
    ):
        raise ValueError("transition_address_forbidden")
    return str(ip)


async def resolve(host):
    domain(host)
    rows = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        ),
        3,
    )
    if not rows or len(rows) > 32:
        raise ValueError("dns_answer_limit")
    # Reject mixed public/private responses rather than selecting the safe subset.
    return sorted({public_ip(row[4][0]) for row in rows})
