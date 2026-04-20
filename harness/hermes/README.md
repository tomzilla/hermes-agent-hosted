# Hermes Harness Container

Containerized Hermes Agent with Slack Webhook support for reproducible deployments.

## Quick Start

### Build the container

```bash
# From repository root
docker build -f harness/hermes/Dockerfile -t hermes-harness:latest .
```

### Run the container (Slack Webhook mode)

```bash
docker run -d \
  --name hermes \
  -v ~/.hermes:/opt/hermes-data \
  -e ORG_ID="your-org-id" \
  -e TENANT_ID="your-tenant-id" \
  -e USER_ID="your-user-id" \
  -e CHANNEL_ID="C12345678" \
  -e SLACK_BOT_TOKEN="xoxb-your-token" \
  -e SLACK_SIGNING_SECRET="your-signing-secret" \
  -e SLACK_ALLOWED_USERS="U12345678" \
  -e STATE_DIR="/var/lib/hermes/state/your-tenant-id" \
  hermes-harness:latest
```

### Using a specific git reference

```bash
docker build -f harness/hermes/Dockerfile \
  --build-arg HERMES_REF=v0.10.0 \
  -t hermes-harness:v0.10.0 .
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ORG_ID` | Yes | Organization identifier |
| `TENANT_ID` | Yes | Tenant identifier (used for state isolation) |
| `USER_ID` | Yes | User identifier |
| `CHANNEL_ID` | Yes | Slack channel ID (e.g., "C12345678") |
| `SLACK_BOT_TOKEN` | Yes | Slack bot token (xoxb-...) |
| `SLACK_SIGNING_SECRET` | Yes | Slack signing secret for HMAC validation |
| `SLACK_ALLOWED_USERS` | No | Comma-separated list of allowed Slack user IDs |
| `STATE_DIR` | No | State directory path (default: `/var/lib/hermes/state`) |
| `HERMES_GATEWAY_BUSY_INPUT_MODE` | No | Input mode (default: "hidden") |

### LLM Provider Variables

At minimum, you need one LLM provider API key:
- `ANTHROPIC_API_KEY` - Anthropic Claude
- `OPENROUTER_API_KEY` - OpenRouter (multiple models)
- Or other supported providers

## Slack App Setup

1. Go to https://api.slack.com/apps
2. Create a new app "From scratch"
3. Add these OAuth scopes:
   - `chat:write` - Send messages
   - `files:write` - Upload files
   - `users:read` - Get user info
   - `channels:read` - Get channel info
   - `groups:read` - Get private channel info
   - `im:read` - Read DM metadata
4. Install the app to your workspace
5. Copy the Bot User OAuth Token to `SLACK_BOT_TOKEN`
6. Find the Signing Secret in "Basic Information" settings
7. Get the channel ID by right-clicking a channel → "Copy link" (the `C...` part)

### Configure Event Subscriptions

1. Go to **Event Subscriptions**
2. Enable events
3. Set Request URL: `http://your-server:8645/webhooks/slack`
4. Subscribe to bot events:
   - `message.channels` - Channel messages
   - `message.groups` - Private channel messages
   - `message.im` - DM messages
   - `message.mpim` - Group DM messages

## Architecture

```
┌────────────────────────────────────────┐
│  Container                             │
│  ┌──────────────────────────────────┐  │
│  │ /opt/hermes-source               │  │
│  │   - Hermes Agent (git checkout)  │  │
│  │   - ./hermes (entrypoint)        │  │
│  │   - ./setup-hermes.sh            │  │
│  │   - venv/ (Python virtualenv)    │  │
│  │   - Slack Webhook Adapter        │  │
│  └──────────────────────────────────┘  │
│  ┌──────────────────────────────────┐  │
│  │ /opt/hermes-data (volume mount)  │  │
│  │   - ~/.hermes config             │  │
│  │   - Sessions                     │  │
│  │   - Cache                        │  │
│  └──────────────────────────────────┘  │
└────────────────────────────────────────┘
            │
            ▼
    ┌───────────────┐
    │ Slack Events  │
    │ API Webhook   │
    │ :8645         │
    └───────────────┘
```

## Exposing the Webhook

To receive Slack events, the container must be reachable:

```bash
# With port mapping
docker run -d \
  --name hermes \
  -p 8645:8645 \
  -v ~/.hermes:/opt/hermes-data \
  -e CHANNEL_ID="C12345678" \
  -e SLACK_BOT_TOKEN="xoxb-..." \
  -e SLACK_SIGNING_SECRET="..." \
  hermes-harness:latest
```

## Troubleshooting

### Permission issues

```bash
# Run with explicit user ID
docker run -v ~/.hermes:/opt/hermes-data -u $(id -u):$(id -g) hermes-harness:latest
```

### Build fails

```bash
# Ensure git can access the repository
docker build -f harness/hermes/Dockerfile --progress=plain -t hermes-harness:latest .
```

### Webhook not receiving events

1. Verify port 8645 is exposed and reachable from the internet
2. Check Slack Event Subscriptions Request URL is correct
3. Check container logs: `docker logs hermes`
