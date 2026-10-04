class ScraperBlockedError(Exception):
    """The site has likely blocked/challenged us (403, 429, CAPTCHA marker,
    etc). The orchestrator stops the whole batch when this fires -- it
    signals the IP/session is burned, not that one lookup failed."""


class ScraperNotFoundError(Exception):
    """The site responded normally but the reference number has no
    tracking data. A legitimate miss, not a block -- safe to continue
    the batch."""


class CarrierConfigError(Exception):
    """A carrier's YAML config is missing, malformed, or missing a
    registered normalizer."""
