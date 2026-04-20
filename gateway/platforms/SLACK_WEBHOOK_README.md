# Slack Webhook Platform

Slack platform adapter using HTTP webhooks (not Socket Mode).

## Features

- Receives Slack events via HTTP POST webhook
- Sends all messages to a single configured channel (`CHANNEL_ID`)
- Supports message.groups, message.im, message.mpim events
- File/image/audio attachment support
- Thread support
- HMAC signature validation

## Requirements

- `aiohttp` - Async HTTP server
- `httpx` - Async HTTP client

Install:
```bash
pip install aiohttp httpx
```

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_BOT_TOKEN` | Yes | Bot token (xoxb-...) |
| `SLACK_SIGNING_SECRET` | Yes | Signing secret for request verification |
| `CHANNEL_ID` | Yes | Destination channel ID (e.g., "C12345678") |

### config.yaml

```yaml
platforms:
  slack_webhook:
    enabled: true
    extra:
      port: 8645  # Webhook server port (default)
```

## Slack App Setup

### 1. Create a Slack App

1. Go to https://api.slack.com/apps
2. Click "Create New App" → "From scratch"
3. Select your workspace

### 2. Add Bot Permissions

**OAuth & Permissions** → Add these scopes:
- `chat:write` - Send messages
- `files:write` - Upload files
- `users:read` - Get user info
- `channels:read` - Get channel info
- `groups:read` - Get private channel info
- `im:read` - Read DM metadata

### 3. Install App

**Install App** → Install to Workspace

Copy the **Bot User OAuth Token** (`xoxb-...`)

### 4. Enable Events

**Event Subscriptions** → Enable

- Request URL: `http://your-server:8645/webhooks/slack`
- Subscribe to bot events:
  - `message.channels` - Channel messages
  - `message.groups` - Private channel messages
  - `message.im` - DM messages
  - `message.mpim` - Group DM messages

### 5. Configure Environment

```bash
export SLACK_BOT_TOKEN=xoxb-your-token-here
export SLACK_SIGNING_SECRET=your-signing-secret
export CHANNEL_ID=C12345678  # Your channel ID
```

## Testing

### 1. Start the Gateway

```bash
python -m gateway.run
```

Expected log:
```
[SlackWebhook] Authenticated as UXXXXX, sending to channel CXXXXX
[SlackWebhook] Listening on port 8645
```

### 2. Verify Health Endpoint

```bash
curl http://localhost:8645/webhooks/slack
```

Expected response:
```json
{"status": "ok", "platform": "slack_webhook", "channel_id": "C12345678"}
```

### 3. Test URL Verification

Slack will send a `url_verification` event when you save the Request URL.

### 4. Send a Test Message

Send a message in the configured Slack channel. The bot should process it.

## Architecture

```
Slack Events API → POST /webhooks/slack:8645 → SlackWebhookAdapter
                                                   │
                                                   ├─→ Verify signature
                                                   ├─→ Parse event
                                                   ├─→ Download files (if any)
                                                   └─→ Send to gateway
                                                           │
                                                           ▼
                                                   Hermes Agent
                                                           │
                                                           ▼
                                                   Send to CHANNEL_ID
```

## Differences from Socket Mode Adapter

| Feature | Socket Mode | Webhook |
|---------|-------------|---------|
| Connection | WebSocket | HTTP POST |
| Real-time | Yes | Depends on Slack delivery |
| Bot user ID detection | Auto | Auto via auth.test |
| Multi-workspace | Yes | Single workspace |
| Message reception | Events API | Events API via webhook |
| Message sending | Same | Same |

## Troubleshooting

### Invalid signature

- Ensure `SLACK_SIGNING_SECRET` matches exactly
- Check system clock is synchronized

### CHANNEL_ID not set

- Find channel ID by right-clicking a channel → "Copy link"
- The ID is the part after `C` (e.g., `C12345678`)

### Messages not being received

- Check Slack app Event Subscriptions are enabled
- Verify Request URL is reachable from the internet
- Check firewall rules for port 8645

### Files not being uploaded

- Bot needs `files:write` scope
- Check file size limits (Slack: 20MB for bots)
