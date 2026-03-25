"""
Short.io MCP Server

Analytics and link management tools for Short.io.
Deployed on Prefect Horizon.

Two base URLs (loaded from environment):
    - SHORTIO_API_BASE: Link management (list_domains, list_links)
    - SHORTIO_STATS_BASE: All statistics endpoints
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

# --- Setup ---

load_dotenv()
logger = logging.getLogger("shortio_mcp_server")

SHORTIO_API_KEY   = os.environ["SHORTIO_API_KEY"]
SHORTIO_DOMAIN_ID = int(os.environ.get("SHORTIO_DOMAIN_ID", "0"))
TIMEZONE          = os.environ.get("TIMEZONE", "Europe/London")
API_BASE          = os.environ.get("SHORTIO_API_BASE", "https://api.short.io")
STATS_BASE        = os.environ.get("SHORTIO_STATS_BASE", "https://statistics.short.io/statistics")

mcp = FastMCP("shortio_mcp")


# --- HTTP header helpers ---

def _api_headers() -> Dict[str, str]:
    return {"Authorization": SHORTIO_API_KEY, "accept": "application/json"}


def _stats_headers() -> Dict[str, str]:
    return {
        "Authorization": SHORTIO_API_KEY,
        "accept": "application/json",
        "content-type": "application/json",
    }


# --- Datetime helpers ---

def _parse_datetime_to_unix_ms(dt_str: str) -> int:
    """
    Convert "YYYY-MM-DD HH:MM" (local time) to Unix milliseconds.
    Raises ValueError for invalid format.
    """
    dt_str = dt_str.strip()
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError(
            f"Invalid datetime format. Expected 'YYYY-MM-DD HH:MM' "
            f"(e.g., '2025-03-01 09:00'). Got: '{dt_str}'"
        )
    tz = ZoneInfo(TIMEZONE)
    dt_aware = dt.replace(tzinfo=tz)
    return int(dt_aware.timestamp() * 1000)


def _parse_datetime_to_iso_utc(dt_str: str) -> str:
    """
    Convert "YYYY-MM-DD HH:MM" (local time) to ISO 8601 UTC string.
    E.g., "2025-12-31T16:00:00.000Z"
    """
    dt_str = dt_str.strip()
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError(
            f"Invalid datetime format. Expected 'YYYY-MM-DD HH:MM' "
            f"(e.g., '2025-03-01 09:00'). Got: '{dt_str}'"
        )
    tz = ZoneInfo(TIMEZONE)
    dt_aware = dt.replace(tzinfo=tz)
    dt_utc = dt_aware.astimezone(timezone.utc)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _convert_iso_utc_to_local(dt_str: Optional[str]) -> Optional[str]:
    """
    Convert an ISO 8601 UTC string (Z suffix) to a local time string.
    Output format: "YYYY-MM-DD HH:MM:SS" in TIMEZONE.
    Returns original value if conversion fails or input is None/empty.
    """
    if not dt_str:
        return dt_str
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        dt_local = dt.astimezone(ZoneInfo(TIMEZONE))
        return dt_local.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return dt_str


# --- Response post-processing ---

def _process_domain(d: Dict) -> Dict:
    """Convert ISO UTC timestamps in a domain record to local timestamps."""
    for field in ("createdAt", "updatedAt", "sslCertExpirationDate"):
        if field in d:
            d[field] = _convert_iso_utc_to_local(d[field])
    return d


def _process_link(lnk: Dict) -> Dict:
    """Convert ISO UTC timestamps in a link record to local timestamps."""
    for field in ("createdAt", "updatedAt", "expiresAt", "ttl"):
        if field in lnk and lnk[field] and isinstance(lnk[field], str):
            lnk[field] = _convert_iso_utc_to_local(lnk[field])
    return lnk


def _process_interval(obj: Dict) -> Dict:
    """Convert interval date fields in a stats response to local timestamps."""
    interval = obj.get("interval", {})
    if interval:
        for field in ("startDate", "endDate", "prevStartDate", "prevEndDate"):
            if field in interval:
                interval[field] = _convert_iso_utc_to_local(interval[field])
    return obj


def _process_chart_data(obj: Dict, key: str = "clickStatistics") -> Dict:
    """Convert x timestamps in chart data to local timestamps."""
    stats = obj.get(key)
    if not stats:
        return obj
    # shortio_domain_stats / shortio_link_stats: nested datasets structure
    if isinstance(stats, dict):
        for dataset in stats.get("datasets", []):
            for point in dataset.get("data", []):
                if "x" in point:
                    point["x"] = _convert_iso_utc_to_local(point["x"])
    # shortio_domain_by_interval: flat array
    elif isinstance(stats, list):
        for point in stats:
            if "x" in point:
                point["x"] = _convert_iso_utc_to_local(point["x"])
    return obj


# --- Error handler ---

def _handle_error(e: Exception, tool_name: str) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401:
            return json.dumps({"error": f"{tool_name}: Authentication failed. Check SHORTIO_API_KEY."})
        if code == 403:
            return json.dumps({"error": f"{tool_name}: Access denied. Check API key permissions."})
        if code == 404:
            return json.dumps({"error": f"{tool_name}: Resource not found. Check IDs and parameters."})
        if code == 429:
            return json.dumps({"error": f"{tool_name}: Rate limit exceeded. Wait before retrying."})
        return json.dumps({"error": f"{tool_name}: API error {code}: {e.response.text}"})
    if isinstance(e, httpx.TimeoutException):
        return json.dumps({"error": f"{tool_name}: Request timed out. Try again."})
    if isinstance(e, ValueError):
        return json.dumps({"error": f"{tool_name}: {str(e)}"})
    return json.dumps({"error": f"{tool_name}: Unexpected error: {type(e).__name__}: {str(e)}"})


# --- Shared helpers ---

def _resolve_domain_id(domain_id: Optional[int]) -> int:
    did = domain_id or SHORTIO_DOMAIN_ID
    if not did:
        raise ValueError(
            "domain_id is required. Provide it explicitly or set SHORTIO_DOMAIN_ID env var."
        )
    return did


def _build_custom_dates_ms(params: BaseModel) -> Dict[str, Any]:
    """Build startDate/endDate as Unix ms for POST body stats endpoints."""
    out = {}
    if getattr(params, "start_date", None):
        out["startDate"] = _parse_datetime_to_unix_ms(params.start_date)
    if getattr(params, "end_date", None):
        out["endDate"] = _parse_datetime_to_unix_ms(params.end_date)
    return out


def _build_filter(f: Optional["FilterDict"]) -> Optional[Dict]:
    if f is None:
        return None
    return {k: v for k, v in f.model_dump().items() if v is not None}


# --- Input models ---

class ListDomainsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit: Optional[int] = Field(default=100, ge=1, le=300)


class ListLinksInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    domain_id: Optional[int] = Field(default=None)
    limit: Optional[int] = Field(default=None, ge=1, le=150)
    before_date: Optional[str] = Field(default=None)
    after_date: Optional[str] = Field(default=None)
    date_sort_order: Optional[str] = Field(default=None, pattern="^(asc|desc)$")
    page_token: Optional[str] = Field(default=None)


class DomainStatsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    domain_id: Optional[int] = Field(default=None)
    period: Optional[str] = Field(
        default="last30",
        pattern="^(today|yesterday|week|month|lastmonth|last7|last30|total|custom)$",
    )
    start_date: Optional[str] = Field(default=None)
    end_date: Optional[str] = Field(default=None)
    clicks_chart_interval: Optional[str] = Field(
        default=None, pattern="^(hour|day|week|month)$"
    )


class FilterDict(BaseModel):
    model_config = ConfigDict(extra="allow")
    countries: Optional[List[str]] = None
    browsers: Optional[List[str]] = None
    browserVersions: Optional[List[str]] = None
    socials: Optional[List[str]] = None
    paths: Optional[List[str]] = None
    refhosts: Optional[List[str]] = None
    utmSources: Optional[List[str]] = None
    utmMediums: Optional[List[str]] = None
    utmCampaigns: Optional[List[str]] = None
    statuses: Optional[List[int]] = None
    protos: Optional[List[str]] = None
    methods: Optional[List[str]] = None
    human: Optional[bool] = None


class DomainByIntervalInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    domain_id: Optional[int] = Field(default=None)
    clicks_chart_interval: str = Field(pattern="^(hour|day|week|month)$")
    period: Optional[str] = Field(
        default="last30",
        pattern="^(today|yesterday|week|month|lastmonth|last7|last30|total|custom)$",
    )
    start_date: Optional[str] = Field(default=None)
    end_date: Optional[str] = Field(default=None)
    include: Optional[FilterDict] = Field(default=None)
    exclude: Optional[FilterDict] = Field(default=None)


COLUMN_PATTERN = "^(browser|browser_version|country|city|os|social|refhost|path|path_404|ab_path|utm_source|utm_medium|utm_campaign|goal_completed|method|proto|st|human)$"


class DomainTopInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    domain_id: Optional[int] = Field(default=None)
    column: str = Field(pattern=COLUMN_PATTERN)
    period: Optional[str] = Field(
        default="last30",
        pattern="^(today|yesterday|week|month|lastmonth|last7|last30|total|custom)$",
    )
    start_date: Optional[str] = Field(default=None)
    end_date: Optional[str] = Field(default=None)
    limit: Optional[int] = Field(default=10, ge=1)
    prefix: Optional[str] = Field(default=None)
    include: Optional[FilterDict] = Field(default=None)
    exclude: Optional[FilterDict] = Field(default=None)


class LinkClicksInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    domain_id: Optional[int] = Field(default=None)
    ids: List[str] = Field(min_length=1)
    start_date: Optional[str] = Field(default=None)
    end_date: Optional[str] = Field(default=None)


class LinkStatsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    link_id: str = Field(min_length=1)
    period: Optional[str] = Field(
        default="last30",
        pattern="^(today|yesterday|week|month|lastmonth|last7|last30|total|custom)$",
    )
    start_date: Optional[str] = Field(default=None)
    end_date: Optional[str] = Field(default=None)
    skip_tops: Optional[bool] = Field(default=False)
    clicks_chart_interval: Optional[str] = Field(
        default=None, pattern="^(hour|day|week|month)$"
    )


class LastClicksInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    domain_id: Optional[int] = Field(default=None)
    limit: Optional[int] = Field(default=30, ge=1)
    period: Optional[str] = Field(
        default="last30",
        pattern="^(today|yesterday|week|month|lastmonth|last7|last30|total|custom)$",
    )
    start_date: Optional[str] = Field(default=None)
    end_date: Optional[str] = Field(default=None)
    before_date: Optional[str] = Field(default=None)
    after_date: Optional[str] = Field(default=None)
    include: Optional[FilterDict] = Field(default=None)
    exclude: Optional[FilterDict] = Field(default=None)


# --- Tools ---

@mcp.tool(
    name="shortio_list_domains",
    annotations={
        "title": "List Short.io domains",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def shortio_list_domains(params: ListDomainsInput) -> str:
    """
    List all domains in the Short.io account.

    Returns each domain's ID, hostname, state, and configuration.
    Use this to discover the domain IDs required by all other Short.io tools.

    Args:
        - limit (int, optional): Number of domains to return. Default 100, max 300.

    Returns:
        List of domains, each containing:
        - id (int): Domain ID used by all other shortio tools
        - hostname (str): ASCII domain name (e.g., "go.ahhmazingwellness.com")
        - unicodeHostname (str): Internationalised domain name
        - state (str): DNS configuration status. One of:
              configured, not_configured, not_registered,
              not_verified, registration_pending, extra_records
        - linkType (str): Default slug generation method. One of:
              increment, random, secure, four-char, eight-char, ten-char
        - cloaking (bool): Whether cloaking is enabled for all links
        - hideReferer (bool): Whether referrer is hidden for all links
        - hideVisitorIp (bool): Whether visitor IPs are stored
        - httpsLevel (str): HTTPS enforcement. One of: none, redirect, hsts
        - enableAI (bool): Whether AI features are enabled
        - isFavorite (bool): Whether the domain is marked as favourite
        - exportEnabled (bool): Whether data export is enabled
        - createdAt (str): Creation datetime in local time "YYYY-MM-DD HH:MM:SS"
        - updatedAt (str): Last update datetime in local time "YYYY-MM-DD HH:MM:SS"
        - sslCertExpirationDate (str): SSL cert expiry in local time "YYYY-MM-DD HH:MM:SS"
    """
    logger.info("Tool called: shortio_list_domains")
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{API_BASE}/api/domains",
                headers=_api_headers(),
                params={"limit": params.limit},
                timeout=30,
            )
            r.raise_for_status()
            domains = r.json()
            return json.dumps([_process_domain(d) for d in domains], ensure_ascii=False)
    except Exception as e:
        return _handle_error(e, "shortio_list_domains")


@mcp.tool(
    name="shortio_list_links",
    annotations={
        "title": "List Short.io links",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def shortio_list_links(params: ListLinksInput) -> str:
    """
    List short links for a domain, with optional date filtering and pagination.

    Date conversion brief:
        - before_date and after_date are converted to ISO 8601 UTC strings internally.
        - Format: "YYYY-MM-DD HH:MM"

    Prerequisites:
        - shortio_list_domains: To obtain a valid domain_id

    Args:
        - domain_id (int, optional): Domain ID. Defaults to SHORTIO_DOMAIN_ID env var.
        - limit (int, optional): Number of links to return. Max 150.
        - before_date (str, optional): Return links created before this datetime.
              Format: "YYYY-MM-DD HH:MM"
        - after_date (str, optional): Return links created after this datetime.
              Format: "YYYY-MM-DD HH:MM"
        - date_sort_order (str, optional): Sort direction. One of: asc, desc.
        - page_token (str, optional): Pagination cursor. Pass nextPageToken from
              the previous response to fetch the next page.

    Returns:
        - count (int): Number of links returned
        - nextPageToken (str or null): Pass to page_token for the next page
        - links (list): Each containing:
              - id (str): Link ID used by statistics tools
              - shortURL (str): The shortened URL
              - secureShortURL (str): HTTPS version
              - originalURL (str): Destination URL
              - path (str): Slug portion of the short URL
              - title (str): Link title if set
              - tags (list): Tag strings
              - archived (bool): Whether the link is archived
              - hasPassword (bool): Whether password-protected
              - clicksLimit (int or null): Disables link after this many clicks
              - expiresAt (str or null): Expiry datetime in local time "YYYY-MM-DD HH:MM:SS"
              - utmSource, utmMedium, utmCampaign, utmTerm, utmContent (str): UTM parameters
              - redirectType (str): HTTP redirect code. One of: 301, 302, 307, 308
              - splitURL (str or null): Alternate destination for A/B testing
              - splitPercent (int or null): Percentage of traffic to splitURL. Range 1–100
              - integrationGA (str or null): Google Analytics tag ID
              - integrationFB (str or null): Meta Pixel ID
              - integrationAdroll (str or null): AdRoll integration ID
              - integrationGTM (str or null): Google Tag Manager container ID
              - DomainId (int): Domain this link belongs to
              - createdAt (str): Creation datetime in local time "YYYY-MM-DD HH:MM:SS"
              - updatedAt (str): Last update datetime in local time "YYYY-MM-DD HH:MM:SS"
    """
    logger.info("Tool called: shortio_list_links")
    try:
        domain_id = _resolve_domain_id(params.domain_id)
        query: Dict[str, Any] = {"domain_id": domain_id}
        if params.limit:
            query["limit"] = params.limit
        if params.before_date:
            query["beforeDate"] = _parse_datetime_to_iso_utc(params.before_date)
        if params.after_date:
            query["afterDate"] = _parse_datetime_to_iso_utc(params.after_date)
        if params.date_sort_order:
            query["dateSortOrder"] = params.date_sort_order
        if params.page_token:
            query["pageToken"] = params.page_token

        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{API_BASE}/api/links",
                headers=_api_headers(),
                params=query,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if "links" in data:
                data["links"] = [_process_link(lnk) for lnk in data["links"]]
            return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return _handle_error(e, "shortio_list_links")


@mcp.tool(
    name="shortio_domain_stats",
    annotations={
        "title": "Short.io domain statistics snapshot",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def shortio_domain_stats(params: DomainStatsInput) -> str:
    """
    Get a click statistics snapshot for a domain, including breakdowns by
    browser, country, city, OS, referrer, and social source, plus
    period-over-period change metrics.

    Date conversion brief:
        - start_date and end_date are converted to Unix milliseconds internally.
        - Format: "YYYY-MM-DD HH:MM". Required when period=custom.
        - Set clicks_chart_interval explicitly for predictable chart output.

    Note: GET endpoint. Uses base URL SHORTIO_STATS_BASE

    Prerequisites:
        - shortio_list_domains: To obtain a valid domain_id

    Args:
        - domain_id (int, optional): Domain ID. Defaults to SHORTIO_DOMAIN_ID env var.
        - period (str, optional): Time period. Default: last30.
              One of: today, yesterday, week, month, lastmonth, last7, last30, total, custom.
        - start_date (str, optional): Required when period=custom. Format: "YYYY-MM-DD HH:MM"
        - end_date (str, optional): Required when period=custom. Format: "YYYY-MM-DD HH:MM"
        - clicks_chart_interval (str, optional): Granularity for embedded chart data.
              One of: hour, day, week, month.

    Returns:
        Summary metrics:
        - clicks (int): Total clicks in the period
        - humanClicks (int): Clicks excluding bots
        - prevClicks (int): Total clicks in the previous comparison period
        - prevHumanClicks (int): Human clicks in the previous comparison period
        - links (int): New links created in the period
        - clicksPerLink (str): Average clicks per link
        - humanClicksPerLink (str): Average human clicks per link
        - prevClicksChange (str): % change in total clicks vs previous period
        - humanClicksChange (str): % change in human clicks vs previous period
        - humanClicksChangePositive (bool): True if human clicks increased
        - linksChange (str): % change in new links vs previous period
        - linksChangePositive (bool): True if more links were created
        - clicksPerLinkChange (str): % change in clicks per link

        Period bounds (all converted to local time "YYYY-MM-DD HH:MM:SS"):
        - interval.startDate / endDate: Current period bounds
        - interval.prevStartDate / prevEndDate: Previous period bounds

        Breakdowns (each ranked by click score):
        - browser (list): [{browser, score}]
        - country (list): [{country, countryName, score}]
        - city (list): [{name, city, countryCode, score}]
              city is a Geoname ID integer; name is the human-readable city name
              Note: some cities return null for name and countryCode when unresolved
        - os (list): [{os, score}]
        - referer (list): [{refhost, score}]
              Note: field key is refhost, not referer, in the actual API response
        - social (list): [{social, score}]
        - utm_medium, utm_source, utm_campaign, utm_term, utm_content (list):
              UTM breakdown arrays

        Chart data (populated only when clicks_chart_interval is set):
        - clickStatistics.datasets[0].data (list): [{x, y}]
              x is the local timestamp "YYYY-MM-DD HH:MM:SS", y is the click count as string
    """
    logger.info("Tool called: shortio_domain_stats")
    try:
        domain_id = _resolve_domain_id(params.domain_id)
        query: Dict[str, Any] = {"tz": TIMEZONE}
        if params.period:
            query["period"] = params.period
        if params.clicks_chart_interval:
            query["clicksChartInterval"] = params.clicks_chart_interval
        if params.start_date:
            query["startDate"] = _parse_datetime_to_unix_ms(params.start_date)
        if params.end_date:
            query["endDate"] = _parse_datetime_to_unix_ms(params.end_date)

        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{STATS_BASE}/domain/{domain_id}",
                headers=_api_headers(),
                params=query,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            _process_interval(data)
            _process_chart_data(data, key="clickStatistics")
            return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return _handle_error(e, "shortio_domain_stats")


@mcp.tool(
    name="shortio_domain_by_interval",
    annotations={
        "title": "Short.io domain time-series",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def shortio_domain_by_interval(params: DomainByIntervalInput) -> str:
    """
    Get time-series click data for a domain, grouped by hour, day, week, or month.
    Use for trend analysis and performance charts.

    Supports include/exclude filters to isolate specific traffic segments.

    Date conversion brief:
        - start_date and end_date are converted to Unix milliseconds internally.
        - Format: "YYYY-MM-DD HH:MM". Required when period=custom.

    Note: POST endpoint. Uses base URL SHORTIO_STATS_BASE

    Prerequisites:
        - shortio_list_domains: To obtain a valid domain_id

    Args:
        - domain_id (int, optional): Domain ID. Defaults to SHORTIO_DOMAIN_ID env var.
        - clicks_chart_interval (str, required): Time bucket granularity.
              One of: hour, day, week, month.
        - period (str, optional): Time period. Default: last30.
              One of: today, yesterday, week, month, lastmonth, last7, last30, total, custom.
        - start_date (str, optional): Required when period=custom. Format: "YYYY-MM-DD HH:MM"
        - end_date (str, optional): Required when period=custom. Format: "YYYY-MM-DD HH:MM"
        - include (dict, optional): Filter to include only clicks matching ALL values.
              Keys: countries (list of 2-letter codes), browsers, browserVersions,
              socials, paths, refhosts, utmSources, utmMediums, utmCampaigns,
              statuses (list of HTTP codes), protos, methods, human (bool)
        - exclude (dict, optional): Filter to exclude clicks matching ANY values.
              Same keys as include.

    Returns:
        - clicksStatistics (list): Time-series data points, each containing:
              IMPORTANT: response key is clicksStatistics (with trailing 's'),
              not clickStatistics as the spec states.
              - x (str): Local timestamp "YYYY-MM-DD HH:MM:SS" for start of bucket
              - y (str): Click count as string (e.g., "12")
    """
    logger.info("Tool called: shortio_domain_by_interval")
    try:
        domain_id = _resolve_domain_id(params.domain_id)
        body: Dict[str, Any] = {
            "clicksChartInterval": params.clicks_chart_interval,
            "period": params.period or "last30",
            "tz": TIMEZONE,
        }
        body.update(_build_custom_dates_ms(params))
        inc = _build_filter(params.include)
        exc = _build_filter(params.exclude)
        if inc:
            body["include"] = inc
        if exc:
            body["exclude"] = exc

        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{STATS_BASE}/domain/{domain_id}/by_interval",
                headers=_stats_headers(),
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            # Real response key is clicksStatistics (flat array)
            _process_chart_data(data, key="clicksStatistics")
            return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return _handle_error(e, "shortio_domain_by_interval")


@mcp.tool(
    name="shortio_domain_top",
    annotations={
        "title": "Short.io domain top column values",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def shortio_domain_top(params: DomainTopInput) -> str:
    """
    Get the top N values for a specific analytics column across a domain,
    ranked by click count descending.

    Supports include/exclude filters to isolate specific traffic segments.

    Date conversion brief:
        - start_date and end_date are converted to Unix milliseconds internally.
        - Format: "YYYY-MM-DD HH:MM". Required when period=custom.

    Note: POST endpoint. Uses base URL SHORTIO_STATS_BASE

    Prerequisites:
        - shortio_list_domains: To obtain a valid domain_id

    Args:
        - domain_id (int, optional): Domain ID. Defaults to SHORTIO_DOMAIN_ID env var.
        - column (str, required): Dimension to rank. One of:
              browser, browser_version, country, city, os, social, refhost,
              path, path_404, ab_path, utm_source, utm_medium, utm_campaign,
              goal_completed, method, proto, st, human
        - period (str, optional): Time period. Default: last30.
              One of: today, yesterday, week, month, lastmonth, last7, last30, total, custom.
        - start_date (str, optional): Required when period=custom. Format: "YYYY-MM-DD HH:MM"
        - end_date (str, optional): Required when period=custom. Format: "YYYY-MM-DD HH:MM"
        - limit (int, optional): Number of top values to return. Default 10.
        - prefix (str, optional): Filter column values to those starting with this string.
        - include (dict, optional): Same keys as shortio_domain_by_interval.
        - exclude (dict, optional): Same keys as shortio_domain_by_interval.

    Returns:
        List of ranked values, each containing:
        - column (str): Raw value (e.g. "/wdb26-prereg", "MY", "Chrome")
        - displayName (str): Human-readable name (same as column for most dimensions)
        - score (str): Click count as string (e.g. "147")
        Note: no timestamp fields in this response.
    """
    logger.info("Tool called: shortio_domain_top")
    try:
        domain_id = _resolve_domain_id(params.domain_id)
        body: Dict[str, Any] = {
            "column": params.column,
            "period": params.period or "last30",
            "limit": params.limit or 10,
            "tz": TIMEZONE,
        }
        body.update(_build_custom_dates_ms(params))
        if params.prefix:
            body["prefix"] = params.prefix
        inc = _build_filter(params.include)
        exc = _build_filter(params.exclude)
        if inc:
            body["include"] = inc
        if exc:
            body["exclude"] = exc

        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{STATS_BASE}/domain/{domain_id}/top",
                headers=_stats_headers(),
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            return json.dumps(r.json(), ensure_ascii=False)
    except Exception as e:
        return _handle_error(e, "shortio_domain_top")


@mcp.tool(
    name="shortio_link_clicks",
    annotations={
        "title": "Short.io compare link click counts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def shortio_link_clicks(params: LinkClicksInput) -> str:
    """
    Get total click counts for multiple links in a single request.
    Use to compare performance across links — e.g., different CTAs on a landing page.

    Date conversion brief:
        - start_date and end_date are converted to ISO 8601 UTC strings internally.
        - Format: "YYYY-MM-DD HH:MM". If omitted, returns all-time totals.
        - Note: this endpoint uses ISO 8601 UTC (not Unix ms), unlike other stats tools.

    Note: GET endpoint. Uses base URL SHORTIO_STATS_BASE

    Prerequisites:
        - shortio_list_domains: To obtain a valid domain_id
        - shortio_list_links: To obtain link IDs

    Args:
        - domain_id (int, optional): Domain ID. Defaults to SHORTIO_DOMAIN_ID env var.
        - ids (list of str, required): Link IDs to retrieve click counts for.
        - start_date (str, optional): Format: "YYYY-MM-DD HH:MM"
        - end_date (str, optional): Format: "YYYY-MM-DD HH:MM"

    Returns:
        Dictionary keyed by link ID (str), each value being the total click
        count (int) for that link. Links with no clicks are absent from the response.
        Example: {"lnk_6gBI_JwDW6APuXqCHo2XsDzFMD": 6, "lnk_6gBI_VJBJHPxPtL4XsAGYag6xt": 1}
        Note: no timestamp fields in this response.
    """
    logger.info("Tool called: shortio_link_clicks")
    try:
        domain_id = _resolve_domain_id(params.domain_id)
        query: Dict[str, Any] = {"ids": ",".join(params.ids)}
        if params.start_date:
            query["startDate"] = _parse_datetime_to_iso_utc(params.start_date)
        if params.end_date:
            query["endDate"] = _parse_datetime_to_iso_utc(params.end_date)

        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{STATS_BASE}/domain/{domain_id}/link_clicks",
                headers=_api_headers(),
                params=query,
                timeout=30,
            )
            r.raise_for_status()
            return json.dumps(r.json(), ensure_ascii=False)
    except Exception as e:
        return _handle_error(e, "shortio_link_clicks")


@mcp.tool(
    name="shortio_link_stats",
    annotations={
        "title": "Short.io link statistics snapshot",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def shortio_link_stats(params: LinkStatsInput) -> str:
    """
    Get a detailed click statistics snapshot for a single short link, including
    breakdowns by browser, country, city, OS, referrer, and social source,
    plus period-over-period change metrics.

    Date conversion brief:
        - start_date and end_date are converted to Unix milliseconds internally.
        - Format: "YYYY-MM-DD HH:MM". Required when period=custom.
        - Set clicks_chart_interval explicitly for predictable chart output.

    Note: GET endpoint. Uses base URL SHORTIO_STATS_BASE

    Prerequisites:
        - shortio_list_links: To obtain a valid link_id

    Args:
        - link_id (str, required): Link ID from shortio_list_links.
        - period (str, optional): Time period. Default: last30.
              One of: today, yesterday, week, month, lastmonth, last7, last30, total, custom.
        - start_date (str, optional): Required when period=custom. Format: "YYYY-MM-DD HH:MM"
        - end_date (str, optional): Required when period=custom. Format: "YYYY-MM-DD HH:MM"
        - skip_tops (bool, optional): If true, omits all breakdown tables and returns
              only click counts and interval. Default false.
        - clicks_chart_interval (str, optional): Granularity for embedded chart data.
              One of: hour, day, week, month.

    Returns:
        Summary metrics:
        - totalClicks (int): Total clicks in the period
        - humanClicks (int): Clicks excluding bots
        - totalClicksChange (str): % change in total clicks vs previous period
        - humanClicksChange (str): % change in human clicks vs previous period

        Period bounds (all converted to local time "YYYY-MM-DD HH:MM:SS"):
        - interval.startDate / endDate: Current period bounds
        - interval.prevStartDate / prevEndDate: Previous period bounds

        Breakdowns (omitted if skip_tops=true, each ranked by click score):
        - browser (list): [{browser, score}]
        - country (list): [{country, countryName, score}]
        - city (list): [{name, city, countryCode, score}]
              Note: city is a Geoname ID string in link stats (vs integer in domain stats)
        - os (list): [{os, score}]
        - referer (list): [{referer, score}]
        - social (list): [{social, score}]

        Chart data (populated only when clicks_chart_interval is set):
        - clickStatistics.datasets[0].data (list): [{x, y}]
              x is local time "YYYY-MM-DD HH:MM:SS", y is click count as string
    """
    logger.info("Tool called: shortio_link_stats")
    try:
        query: Dict[str, Any] = {"tz": TIMEZONE}
        if params.period:
            query["period"] = params.period
        if params.skip_tops:
            query["skipTops"] = "true"
        if params.clicks_chart_interval:
            query["clicksChartInterval"] = params.clicks_chart_interval
        if params.start_date:
            query["startDate"] = _parse_datetime_to_unix_ms(params.start_date)
        if params.end_date:
            query["endDate"] = _parse_datetime_to_unix_ms(params.end_date)

        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{STATS_BASE}/link/{params.link_id}",
                headers=_api_headers(),
                params=query,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            _process_interval(data)
            _process_chart_data(data, key="clickStatistics")
            return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return _handle_error(e, "shortio_link_stats")


@mcp.tool(
    name="shortio_last_clicks",
    annotations={
        "title": "Short.io raw click events",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def shortio_last_clicks(params: LastClicksInput) -> str:
    """
    Get a list of the most recent individual click events for a domain,
    each with exact timestamp, device, browser, country, and referrer.
    Use when you need to know the precise date and time a click occurred,
    or to audit specific click events.

    Date conversion brief:
        - start_date, end_date, before_date, and after_date are converted to
              Unix milliseconds internally. Format: "YYYY-MM-DD HH:MM".
        - before_date and after_date are pagination cursors. To page through
              results, extract the dt value from the last record received,
              reformat it as "YYYY-MM-DD HH:MM", and pass it to before_date.
        - dt in the response already carries the local timezone offset
              (e.g., "2026-03-22T04:30:38.549+08:00") — no conversion applied.

    Note: POST endpoint. Uses base URL SHORTIO_STATS_BASE

    Prerequisites:
        - shortio_list_domains: To obtain a valid domain_id

    Args:
        - domain_id (int, optional): Domain ID. Defaults to SHORTIO_DOMAIN_ID env var.
        - limit (int, optional): Number of records to return. Default 30.
        - period (str, optional): Time period. Default: last30.
              One of: today, yesterday, week, month, lastmonth, last7, last30, total, custom.
        - start_date (str, optional): Required when period=custom. Format: "YYYY-MM-DD HH:MM"
        - end_date (str, optional): Required when period=custom. Format: "YYYY-MM-DD HH:MM"
        - before_date (str, optional): Pagination cursor. Format: "YYYY-MM-DD HH:MM"
        - after_date (str, optional): Pagination cursor. Format: "YYYY-MM-DD HH:MM"
        - include (dict, optional): Same keys as shortio_domain_by_interval.
        - exclude (dict, optional): Same keys as shortio_domain_by_interval.

    Returns:
        List of individual click records, each containing:
        - dt (str): Exact timestamp with local timezone offset already applied
              (e.g., "2026-03-22T04:30:38.549+08:00") — no further conversion needed
        - path (str): Short link slug clicked (e.g., "/*", "/wdb26-prereg")
        - lcpath (str): Lowercase version of path
        - human (bool): True if identified as human
        - method (str): HTTP method. One of: GET, POST, PUT, DELETE, HEAD
        - browser (str): Browser name (e.g., "Chrome")
        - browser_version (str): Browser version (e.g., "131")
        - os (str): Operating system (e.g., "Windows")
        - country (str): Country name (e.g., "Malaysia")
        - city (str): City name if available (may be absent for some records)
        - geoname_id (str): Geoname ID of the city
        - ip (str): Visitor IP (last octets may be masked for EU visitors)
        - ref (str): Full referrer URL
        - refhost (str): Referrer hostname only
        - social (str): Social network name if applicable
        - url (str): Full destination URL after redirect
        - host (str): Domain name (e.g., "go.ahhmazingwellness.com")
        - proto (str): Protocol. One of: http, https
        - st (number): HTTP status code (e.g., 302)
        - utm_source, utm_medium, utm_campaign, utm_term, utm_content (str): UTM values
        - ab_path (str or null): A/B test path if applicable
        - goal_completed (str or null): Conversion goal identifier if applicable
        - ja4 (str): JA4 TLS fingerprint
    """
    logger.info("Tool called: shortio_last_clicks")
    try:
        domain_id = _resolve_domain_id(params.domain_id)
        body: Dict[str, Any] = {
            "limit": params.limit or 30,
            "period": params.period or "last30",
            "tz": TIMEZONE,
        }
        if params.start_date:
            body["startDate"] = _parse_datetime_to_unix_ms(params.start_date)
        if params.end_date:
            body["endDate"] = _parse_datetime_to_unix_ms(params.end_date)
        if params.before_date:
            body["beforeDate"] = _parse_datetime_to_unix_ms(params.before_date)
        if params.after_date:
            body["afterDate"] = _parse_datetime_to_unix_ms(params.after_date)
        inc = _build_filter(params.include)
        exc = _build_filter(params.exclude)
        if inc:
            body["include"] = inc
        if exc:
            body["exclude"] = exc

        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{STATS_BASE}/domain/{domain_id}/last_clicks",
                headers=_stats_headers(),
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            return json.dumps(r.json(), ensure_ascii=False)
    except Exception as e:
        return _handle_error(e, "shortio_last_clicks")
