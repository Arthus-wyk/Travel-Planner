# Custom Travel Planner MCP

This project is a small MCP server plus an Ollama-powered client for a custom
travel planning agent.

The MCP server stores travel planning workspaces in MySQL. The agent can use
tools to capture traveler requirements, build day-by-day itineraries, keep
candidate recommendations, and track budget items.

## MySQL

Configure the database connection with environment variables:

```powershell
$env:TRAVEL_DB_HOST = "127.0.0.1"
$env:TRAVEL_DB_PORT = "3306"
$env:TRAVEL_DB_USER = "root"
$env:TRAVEL_DB_PASSWORD = "your-password"
$env:TRAVEL_DB_NAME = "custom_travel_planner"
```

Defaults are localhost, port `3306`, user `root`, empty password, and database
`custom_travel_planner`.

The server checks and creates the database and `travel_plans` table
automatically during startup before exposing MCP tools.

## Google Maps

Configure a Google Maps Platform API key with Places API (New) and Routes API
enabled. You can set it in the shell or in a local `.env` file in the project
root:

```powershell
$env:GOOGLE_MAPS_API_KEY = "your-google-maps-api-key"
```

```text
GOOGLE_MAPS_API_KEY=your-google-maps-api-key
```

Optional:

```powershell
$env:GOOGLE_MAPS_TIMEOUT_SECONDS = "10"
```

## Tools

- `google_maps_search_places`: search Google Places by text query.
- `google_maps_search_hotels`: search hotel candidates in a destination.
- `google_maps_compute_route`: calculate route distance and travel time.
- `delete_itinerary_item`: delete an itinerary item and invalidate related routes.
- `move_itinerary_item`: move an itinerary item to another day/time with conflict checks.
- `reconcile_travel_plan`: refresh affected routes and audit schedule, pace, and budget.
- `update_itinerary_routes`: persist calculated route records on the travel plan.
- `create_travel_plan`: create a planning workspace with destination, dates,
  travelers, budget, interests, and constraints.
- `update_travel_profile`: update high-level trip requirements.
- `add_itinerary_item`: add a scheduled day-by-day activity.
- `update_itinerary_item`: revise an existing itinerary item.
- `list_itinerary`: list all itinerary items or one day of the trip.
- `add_recommendation`: store candidate hotels, restaurants, attractions,
  routes, or experiences before they are scheduled.
- `update_recommendation`: revise a candidate recommendation.
- `add_budget_item`: track budget lines such as flights, hotels, food,
  tickets, and transit.
- `get_travel_plan`: return the full planning workspace.
- `summarize_travel_plan`: return a compact planning summary with estimated
  costs and itinerary coverage.
- `list_travel_plans`: list all current planning workspaces.
- `delete_travel_plan`: delete a planning workspace.

Itinerary items require a concrete 24-hour start time in `HH:MM` format (for
example `09:30`). Items on the same day are returned in chronological order,
and adding or updating an item is rejected when its duration overlaps another
scheduled item. `audit_travel_plan` also reports missing times and conflicts.

## Run

The agent uses Ollama Cloud by default. Set an Ollama API key before starting
the client or API server:

```powershell
$env:OLLAMA_API_KEY = "your-ollama-api-key"
```

The default cloud model is `gpt-oss:120b-cloud`. `OLLAMA_HOST` can be set to override the
Cloud endpoint when needed.

Start the interactive agent:

```powershell
uv run python client.py
```

Or run the MCP server directly over stdio:

```powershell
uv run python server.py
```

Start the HTTP API bridge:

```powershell
uv run python api_server.py
```

After installation, the equivalent explicit commands are `travel-agent`,
`travel-mcp`, and `travel-api`.

The canonical architecture separates orchestration, tool contracts, business
capabilities, and infrastructure:

- `agent/`: loop entry point, routing, execution, and confirmation/permission/risk policy.
- `tooling/`: transport-independent tool specs, request context, registry, and errors.
- `tools/`: real business domains (`plan`, `itinerary`, `maps`, `recommendation`, and `memory`).
  Every domain has `schemas.py`, `service.py`, and `mcp_tools.py`.
- `infrastructure/`: database/repository and external-client adapters.
- `mcp_server.py`: the MCP composition root and stdio transport entry point.

`service/` remains as a compatibility namespace for the HTTP API and mature
travel-planning implementation while imports migrate to the new boundaries.
Importing it has no MCP registration side effects. The project has one MCP
composition root (`mcp_server.py`) and one tool catalog (`tools/catalog.py`).
Root `server.py` starts that composition root.

Data is stored in MySQL, so travel plans persist across server restarts.
