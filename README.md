# mcp-server-shortio

FastMCP server providing analytics and link management tools for Short.io.
Deployed on Prefect Horizon.

## Tools

| Tool | Method | Endpoint | Description |
|---|---|---|---|
| `shortio_list_domains` | GET | `/api/domains` | List all domains |
| `shortio_list_links` | GET | `/api/links` | List links for a domain |
| `shortio_domain_stats` | GET | `/statistics/domain/{id}` | Domain statistics snapshot |
| `shortio_domain_by_interval` | POST | `/statistics/domain/{id}/by_interval` | Domain time-series |
| `shortio_domain_top` | POST | `/statistics/domain/{id}/top` | Domain top column values |
| `shortio_link_clicks` | GET | `/statistics/domain/{id}/link_clicks` | Compare multiple link click counts |
| `shortio_link_stats` | GET | `/statistics/link/{id}` | Link statistics snapshot |
| `shortio_last_clicks` | POST | `/statistics/domain/{id}/last_clicks` | Raw click events |

## Known broken endpoints

Two Short.io API endpoints were confirmed broken during testing and are not
implemented in this server. Workarounds using domain-level tools are documented below.

### `POST /statistics/link/{id}/by_interval`
Returns HTTP 404 for all link IDs tested. The endpoint does not exist despite
appearing in Short.io's OpenAPI documentation.

**Workaround**: Use `shortio_domain_by_interval` with a `paths` filter:
```
shortio_domain_by_interval(
    clicks_chart_interval="day",
    period="last30",
    include={"paths": ["/your-link-path"]}
)
```

### `POST /statistics/link/{id}/top`
Returns HTTP 500 for all link IDs tested. Confirmed across multiple links.

**Workaround**: Use `shortio_domain_top` with a `paths` filter:
```
shortio_domain_top(
    column="country",
    period="last30",
    include={"paths": ["/your-link-path"]}
)
```

## Deployment (Prefect Horizon)

- Entrypoint: `shortio_server.py:mcp`
- Environment secrets: `SHORTIO_API_KEY`, `SHORTIO_DOMAIN_ID`, `SHORTIO_API_BASE`, `SHORTIO_STATS_BASE`, `TIMEZONE`

## Local setup

```bash
pip install -r requirements.txt
cp env.template .env
# Fill in your values in .env
fastmcp inspect shortio_server.py:mcp
```
