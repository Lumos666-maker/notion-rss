"""Normalize article URLs so tracking parameters don't defeat dedupe."""

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Query keys that are always tracking. `f` is handled separately: only `f=rss`.
_TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "gclsrc",
    "dclid",
    "gbraid",
    "wbraid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "igsh",
    "_ga",
    "_gl",
    "yclid",
    "twclid",
    "ttclid",
    "li_fat_id",
    "mkt_tok",
    "oly_enc_id",
    "oly_anon_id",
    "vero_conv",
    "vero_id",
    "nr_email_referer",
    "ncid",
    "spm",
    "scm",
    "share_source",
    "share_token",
    "share_id",
    "ref_src",
    "ref_url",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_name",
    "utm_reader",
    "utm_viz_id",
    "utm_pubreferrer",
    "utm_swu",
    "utm_referrer",
    "fb_action_ids",
    "fb_action_types",
    "fb_source",
    "fb_ref",
    "at_medium",
    "at_campaign",
    "at_custom1",
    "at_custom2",
    "at_custom3",
    "at_custom4",
}

# Values that mark a generic param as a feed referral rather than content.
_RSS_VALUES = {"rss", "rssfeed", "feed"}
_RSS_VALUE_KEYS = {"f", "from", "source", "ref"}


def normalize_url(url: str) -> str:
    """Strip utm_*, f=rss, and similar tracking params. Return "" for empty input.

    Non-tracking query parameters are kept, so article ids survive. The host is
    lowercased so the same link with a different host case compares equal.
    """
    if not url:
        return ""
    raw = url.strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return raw

    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered.startswith("hsa_") or lowered.startswith("_hs"):
            continue
        if lowered in _TRACKING_KEYS:
            continue
        if lowered in _RSS_VALUE_KEYS and value.lower() in _RSS_VALUES:
            continue
        kept.append((key, value))

    fragment = parsed.fragment
    if fragment.lower().startswith("utm_") or fragment.lower().startswith("xtor"):
        fragment = ""

    netloc = parsed.netloc.lower()
    if parsed.scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif parsed.scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]

    path = parsed.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((
        parsed.scheme.lower(),
        netloc,
        path,
        "",
        urlencode(kept, doseq=True),
        fragment,
    ))
