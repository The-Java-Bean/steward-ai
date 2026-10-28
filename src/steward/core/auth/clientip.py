# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Which address a request really came from, given the configured trusted proxies."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network
type Address = ipaddress.IPv4Address | ipaddress.IPv6Address

UNKNOWN = "unknown"


def _parse(value: str) -> Address | None:
    value = value.strip()
    if value.startswith("[") and "]" in value:  # [v6]:port
        value = value[1 : value.index("]")]
    elif value.count(":") == 1:  # v4:port
        value = value.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _trusted(address: Address, proxies: Sequence[Network]) -> bool:
    return any(address.version == net.version and address in net for net in proxies)


def client_ip(
    peer: str | None, forwarded_for: Iterable[str] = (), *, trusted: Sequence[Network] = ()
) -> str:
    """The client address for logging and rate limiting.

    `peer` is the address that connected; `forwarded_for` holds the `X-Forwarded-For`
    header values in order. The header is believed only hop by hop from the right, and
    only while each hop is a trusted proxy, so a client can't pick its own address by
    sending the header itself.
    """
    current = _parse(peer) if peer else None
    if current is None:
        return UNKNOWN
    hops = [hop for value in forwarded_for for hop in value.split(",") if hop.strip()]
    while hops and _trusted(current, trusted):
        previous = _parse(hops.pop())
        if previous is None:
            break  # garbage from the far side of the proxy: stop at the proxy
        current = previous
    return str(current)


def is_trusted_proxy(peer: str | None, *, trusted: Sequence[Network] = ()) -> bool:
    """Whether the address that connected is one of the trusted proxies."""
    address = _parse(peer) if peer else None
    return address is not None and _trusted(address, trusted)
