"""Source policy for optional authentication on self-hosted connectors."""

from collections.abc import Iterable
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network

_LOCAL_NETWORKS = tuple(ip_network(net) for net in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7",
))


def _address(value: str) -> IPv4Address | IPv6Address:
    if "%" in value:
        raise ValueError("IP zone identifiers are not supported")
    address = ip_address(value)
    return (address.ipv4_mapped or address) if isinstance(address, IPv6Address) else address


def parse_source_ips(value: str) -> frozenset[IPv4Address | IPv6Address]:
    return frozenset(_address(part.strip()) for part in value.split(",") if part.strip())


def source_allowed(
    peer: str | None, headers: Iterable[str], *, has_credential: bool, allowed_ips: str = "",
) -> bool:
    if has_credential and not allowed_ips:
        return True
    # Uvicorn can replace request.client using X-Forwarded-For. Never use a
    # forwarded request as evidence of a direct or allowlisted peer.
    header_names = {name.lower() for name in headers}
    if not peer or header_names.intersection({"forwarded", "x-real-ip", "via"}) or any(
        name.startswith("x-forwarded-") for name in header_names
    ):
        return False
    # A web page on the LAN user's browser is not a connector peer.
    if not has_credential and ("origin" in header_names or "sec-fetch-site" in header_names):
        return False
    try:
        address = _address(peer)
        if allowed_ips and address not in parse_source_ips(allowed_ips):
            return False
    except ValueError:
        return False
    return has_credential or any(address in network for network in _LOCAL_NETWORKS)
