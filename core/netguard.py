"""
One SSRF guard for every user-supplied outbound URL.

Operators type endpoint URLs into console forms — S3 endpoints, notification
webhooks, vector stores, Brain import URLs — and the web or worker process then
makes requests to them. A URL that points back into the deployment's own network
(loopback, RFC-1918, link-local) would let a console user probe or drive services
only the server can reach, so every such URL is checked here, in one place:

* ``PYRUNNER_ALLOW_PRIVATE_NETWORK`` **off (the default)** — loopback, private,
  link-local and reserved addresses are rejected. The hostname is DNS-resolved
  first, so a public name that points at a private IP is caught as well as a
  literal private IP.
* **on** — loopback, private and reserved addresses are allowed, so a Qdrant,
  MinIO, ntfy or Ollama sibling on the compose network (or on localhost during
  development) is reachable. Only the operator can set it (it is an environment
  variable, never a console setting), so the people the guard defends against
  cannot flip it.
* **Always blocked, whatever the flag says** — the link-local / cloud-metadata
  ranges (``169.254.0.0/16``: ``169.254.169.254`` serves instance credentials on
  AWS, GCP and Azure; ``fe80::/10``; AWS IMDS over IPv6 ``fd00:ec2::254``) and the
  unspecified addresses ``0.0.0.0`` / ``::``. Reaching your own MinIO is a
  legitimate operator choice; leaking the host's cloud credentials never is.

``core.services.s3_service.is_safe_endpoint_url`` re-exports the function so the
original import path keeps working.
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from django.conf import settings

logger = logging.getLogger(__name__)

#: Never reachable, regardless of PYRUNNER_ALLOW_PRIVATE_NETWORK.
ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),  # IPv4 link-local, incl. cloud metadata
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IMDS over IPv6 (inside the ULA range)
)

_LOCALHOST_NAMES = ("localhost", "localhost.localdomain")
_UNSPECIFIED_NAMES = ("0.0.0.0", "::", "[::]")

FLAG_HINT = " (set PYRUNNER_ALLOW_PRIVATE_NETWORK=true to allow private-network endpoints)"


def private_network_allowed() -> bool:
    """The operator's stance, read at call time so tests can override it."""
    return bool(getattr(settings, "PYRUNNER_ALLOW_PRIVATE_NETWORK", False))


def is_safe_endpoint_url(url: str, *, allow_private: bool | None = None) -> tuple[bool, str]:
    """
    Decide whether PyRun may make outbound requests to ``url``.

    Returns ``(is_safe, error_message)``; the message is user-facing and names
    the offending address. An empty URL is safe (callers treat it as "unset"). A
    hostname that does not resolve is allowed with a warning, as before — custom
    DNS inside a deployment is a legitimate setup and the request itself will
    fail loudly if the name is wrong.

    ``allow_private`` defaults to ``settings.PYRUNNER_ALLOW_PRIVATE_NETWORK``;
    pass it explicitly to pin a stricter (or looser) policy for one caller.
    """
    if not url:
        return True, ""
    if allow_private is None:
        allow_private = private_network_allowed()

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False, "Invalid URL: no hostname found"

        host = hostname.lower()
        if host in _UNSPECIFIED_NAMES:
            return False, "Unspecified addresses are not allowed (0.0.0.0 / ::)"
        if host in _LOCALHOST_NAMES or host.endswith(".localhost"):
            # RFC 6761: *.localhost is loopback by definition — no DNS needed.
            if allow_private:
                return True, ""
            return False, "Internal endpoints are not allowed (localhost)" + FLAG_HINT

        try:
            addr_info = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            logger.warning("Endpoint hostname does not resolve: %s", hostname)
            return True, ""

        for *_, sockaddr in addr_info:
            ok, message = _check_ip(sockaddr[0], allow_private)
            if not ok:
                return False, message
        return True, ""

    except Exception as e:
        return False, f"Invalid endpoint URL: {e}"


def _check_ip(ip_str: str, allow_private: bool) -> tuple[bool, str]:
    # getaddrinfo may return a scoped IPv6 literal ("fe80::1%eth0"); the zone id
    # is not part of the address.
    ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
    # An IPv4-mapped IPv6 address (::ffff:169.254.169.254) is the IPv4 address.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped

    if ip.is_unspecified:
        return False, f"Unspecified addresses are not allowed ({ip_str})"
    if ip.is_link_local or any(ip in net for net in ALWAYS_BLOCKED_NETWORKS):
        return False, f"Link-local / cloud-metadata addresses are never allowed ({ip_str})"
    if allow_private:
        return True, ""
    if ip.is_loopback:
        return False, f"Loopback addresses are not allowed ({ip_str})" + FLAG_HINT
    if ip.is_private:
        return False, f"Private IP addresses are not allowed ({ip_str})" + FLAG_HINT
    if ip.is_reserved:
        return False, f"Reserved addresses are not allowed ({ip_str})" + FLAG_HINT
    return True, ""
