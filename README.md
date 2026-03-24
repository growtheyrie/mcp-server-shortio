# Short.io MCP Server

FastMCP server providing analytics and link management tools for Short.io.
Deployed on Prefect Horizon.

## Tools

| Tool | Endpoint | Description |
|---|---|---|
| `shortio_list_domains` | `GET /api/domains` | List all domains |
| `shortio_list_links` | `GET /api/links` | List links for a domain |
| `shortio_domain_stats` | `GET /statistics/domain/{id}` | Domain statistics snapshot |
| `shortio_domain_by_interval` | `POST /statistics/domain/{id}/by_interval` | Domain time-series |
| `shortio_domain_top` | `POST /statistics/domain/{id}/top` | Domain top column values |
| `shortio_link_clicks` | `GET /statistics/domain/{id}/link_clicks` | Compare multiple link click counts |
| `shortio_link_stats` | `GET /statistics/link/{id}` | Link statistics snapshot |
| `shortio_link_top` | `POST /statistics/link/{id}/top` | Link top column values |
| `shortio_link_by_interval` | `POST /statistics/link/{id}/by_interval` | Link time-series |
| `shortio_last_clicks` | `POST /statistics/domain/{id}/last_clicks` | Raw click events |

## Deployment (Prefect Horizon)

- Entrypoint: `shortio_server.py:mcp`
- Environment secrets: `SHORTIO_API_KEY`, `SHORTIO_DOMAIN_ID`, `TIMEZONE`

## Local setup

```bash
pip install -r requirements.txt
cp .env.template .env
# Fill in your values in .env
fastmcp inspect shortio_server.py:mcp
```
