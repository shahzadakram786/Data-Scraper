"""Typed failures for the scraping engines.

The point of these is that a run that goes wrong says so. Before this existed,
a Psychology Today listing page that parsed to zero providers was
indistinguishable from "you have reached the end of the directory", so a broken
scraper finished with a cheerful "Collection finished" and wrote nothing. Four
runs on 2026-08-10 and 2026-08-20 did exactly that.

Anything raised from here is a run-ending condition the user needs to see, not
a per-provider hiccup — those stay warnings in the log.
"""


class ScraperError(Exception):
    """Base for run-ending scraper failures.

    `remedy` is the sentence shown to the user under the error banner: what
    they can actually do about it. Keep it actionable.
    """

    remedy = ""

    def __init__(self, message, remedy=None):
        super().__init__(message)
        if remedy is not None:
            self.remedy = remedy


class RegionBlockedError(ScraperError):
    """Psychology Today refused the request on geography and the free reader
    relay could not stand in for it either."""

    remedy = ("Psychology Today only serves its directory to US traffic, and the "
              "free r.jina.ai reader fallback also failed. Set a US proxy/VPN in "
              "Settings > Proxy and try again.")


class ListingParseError(ScraperError):
    """A page was fetched successfully but produced no usable records.

    Almost always means the site changed its markup and a parser needs
    updating — which is worth knowing immediately rather than after a
    40-minute run that saved nothing.
    """

    remedy = ("The page loaded but no providers could be read out of it, which "
              "usually means the site changed its markup. Run Tools > Health "
              "Check for details before starting another run.")


class SourceUnavailableError(ScraperError):
    """The source could not be reached at all (DNS, connectivity, hard block)."""

    remedy = ("The source could not be reached. Check your internet connection, "
              "then run Tools > Health Check.")
