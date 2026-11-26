# MCP server

## What it is

A small server that lets an AI app (Claude or another MCP client) use this toolkit directly. It does
not add features. Each tool runs the matching `maps` command and returns its JSON. The rules do not
change: searches are free unless the request says `cost: quality` with a dollar ceiling, and every
search reports what it did not cover.

Tools: `maps_doctor`, `maps_budget`, `maps_area`, `maps_search`, `maps_rank`, `maps_followups`,
`maps_census`, `maps_details`, `maps_reviews_fetch`, `maps_reviews_search`, `maps_reviews_integrity`,
`maps_reviews_contributors`, `maps_runs`, `maps_purge`. It also serves the research method
(`maps://skill`) and the repo rules (`maps://agents`) as readable resources.

## Run it on your own computer (stdio)

Install the extra once: `uv sync --extra mcp`. Then a local client config points at the server like
this (the exact file depends on the app):

    {
      "mcpServers": {
        "maps-research": {
          "command": "uv",
          "args": ["run", "--extra", "mcp", "python", "-m", "maps.mcp_server"],
          "cwd": "/path/to/maps-research",
          "env": { "GOOGLE_MAPS_API_KEY": "your key" }
        }
      }
    }

Leave the key out to use only the offline simulator (`providers: ["sim"]`).

## Run it over HTTP

    export MAPS_MCP_TOKEN="a long random string"
    uv run --extra mcp python -m maps.mcp_server --http --host 0.0.0.0 --port 8080

The address is `http://localhost:8080/mcp`. Every request must send
`Authorization: Bearer <your token>`; anything else gets 401. Without `MAPS_MCP_TOKEN` the server
refuses to start (exit code 2). The token is never printed.

## Docker

    docker build -t maps-mcp .
    docker run --rm -p 8080:8080 -e MAPS_MCP_TOKEN="..." -e GOOGLE_MAPS_API_KEY="..." \
      -v maps-data:/data maps-mcp

State is kept in `/data` (`MAPS_STATE_DIR`). The container listens on `$PORT`, default 8080.

## Deploying to Google Cloud Run

Run from the repo root, with the two secrets already stored in Secret Manager:

    gcloud run deploy maps-mcp --source . --region REGION \
      --allow-unauthenticated \
      --set-secrets GOOGLE_MAPS_API_KEY=GOOGLE_MAPS_API_KEY:latest,MAPS_MCP_TOKEN=MAPS_MCP_TOKEN:latest

`--no-allow-unauthenticated` is not used: Claude connects with the bearer token, not with Google
sign-in, so Cloud Run must let requests through and the token is the only lock. Use a long random
token.

Then in Claude, add a custom connector with the URL `https://YOUR-SERVICE-URL/mcp` and give it the
bearer token as its credential.

## Caveat: the disk is temporary

A Cloud Run container's disk disappears when the instance stops. Stored runs and the usage ledger
reset between instances, so the toolkit's own budget guard undercounts. The daily quota cap you set
in the Google console is the real spend limit. See [setup.md](setup.md).

## Status

Built and tested in-process only. Not deployed, and not tested against a real MCP client or Cloud Run.
