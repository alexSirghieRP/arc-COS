"""Corporate TLS trust.

The company network does TLS interception with a root CA that isn't in
certifi's bundle, so every REST call to Atlassian/Azure fails with
CERTIFICATE_VERIFY_FAILED (the Confluence feed and knowledge sync have been
silently returning empty because of this). The corporate root IS trusted by
the macOS keychain, so we build a combined bundle once per process --
certifi + the keychain roots -- and point both urllib (via SSL_CERT_FILE) and
our httpx clients (via ca_bundle()) at it.

No-op cost on machines without the corp CA: the bundle is just certifi plus
whatever the keychain holds.
"""

import functools
import logging
import os
import subprocess
import tempfile

import certifi

log = logging.getLogger("chief.certs")

_KEYCHAINS = [
    "/Library/Keychains/System.keychain",
    "/System/Library/Keychains/SystemRootCertificates.keychain",
]


@functools.lru_cache(maxsize=1)
def ca_bundle() -> str:
    """Path to a combined CA bundle (certifi + macOS keychain roots), built once."""
    parts = [open(certifi.where(), encoding="utf-8").read()]
    for kc in _KEYCHAINS:
        try:
            out = subprocess.run(
                ["security", "find-certificate", "-a", "-p", kc],
                capture_output=True, text=True, timeout=30)
            if out.returncode == 0 and "BEGIN CERTIFICATE" in out.stdout:
                parts.append(out.stdout)
        except Exception as e:  # non-macOS, or security unavailable: certifi-only
            log.warning("keychain export failed for %s: %s", kc, e)
    path = os.path.join(tempfile.gettempdir(), "chief-ca-bundle.pem")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    n = sum(p.count("BEGIN CERTIFICATE") for p in parts)
    log.info("CA bundle built: %s (%d certs)", path, n)
    return path


def install() -> None:
    """Point stdlib urllib/ssl and requests at the combined bundle via env vars.
    httpx callers pass ca_bundle() explicitly. Safe to call multiple times."""
    bundle = ca_bundle()
    os.environ["SSL_CERT_FILE"] = bundle
    os.environ["REQUESTS_CA_BUNDLE"] = bundle
