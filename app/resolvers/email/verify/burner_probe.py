"""Rung 4 — burner-domain probe. Documented, built, and shipped disabled.

For the addresses the first three rungs cannot settle (catch-all domains,
Google/Microsoft tenants), the only remaining ground truth is to actually
send a message and see whether it bounces. That is a real send with real
reputational consequences, so it happens from a throwaway domain that
exists for nothing else.

**Never probe from a sending domain.** The whole point is that the bounces
land on a reputation we are willing to destroy. Pointing this at a domain
that also sends campaigns would burn the asset the entire product depends
on, quietly, over weeks. That is not a warning in this module — it is a
constructor that refuses to build.

Disabled by default (`enabled=False`). Enabling it is a deliberate act
with a cost, not a default anyone backs into.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.errors import PolicyBlockedError


@dataclass(frozen=True, slots=True)
class BurnerProbeConfig:
    enabled: bool = False
    probe_domain: str | None = None
    # Every domain the product sends real campaigns from. The probe domain
    # must not be one of them.
    sending_domains: frozenset[str] = field(default_factory=frozenset)


class BurnerProber:
    """Constructing this with a misconfiguration raises immediately, at
    startup, rather than at first use — a bad probe domain must not be
    discoverable only after it has already sent mail.
    """

    def __init__(self, config: BurnerProbeConfig) -> None:
        self._config = config

        if not config.enabled:
            return

        if not config.probe_domain:
            raise PolicyBlockedError(
                "burner probe is enabled but no probe_domain is configured — "
                "refusing to guess which domain is disposable"
            )

        probe_domain = config.probe_domain.strip().lower()
        sending = {domain.strip().lower() for domain in config.sending_domains}
        if probe_domain in sending:
            raise PolicyBlockedError(
                f"burner probe domain {probe_domain!r} is also a sending domain. "
                "Bounces from this probe must land on a disposable reputation, "
                "never on the domains that carry real campaigns."
            )

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def probe_domain(self) -> str | None:
        return self._config.probe_domain
