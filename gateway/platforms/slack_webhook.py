"""
Slack Webhook platform adapter.

Receives Slack events via HTTP webhook (not Socket Mode) and sends messages
to a single configured channel.

Uses Slack's Events API with HTTP request mode:
- POST /webhooks/slack receives events
- Validates request signature
- Processes message events
- Sends responses to CHANNEL_ID

Required environment variables:
    SLACK_BOT_TOKEN - Bot token (xoxb-...) for API calls
    SLACK_SIGNING_SECRET - Signing secret for request verification
    CHANNEL_ID - Slack channel ID to send messages to (e.g., "C12345678")

Features:
    - Single channel output (defined by CHANNEL_ID)
    - Receives message.groups, message.im, message.mpim events
    - File/image/audio attachments support
    - Thread support (replies in thread)
    - HMAC signature validation
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, List, Tuple

try:
    from aiohttp import web
    import httpx
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None
    httpx = None

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    SUPPORTED_DOCUMENT_TYPES,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_audio_from_bytes,
)

logger = logging.getLogger(__name__)


def check_slack_webhook_requirements() -> bool:
    """Check if Slack webhook dependencies are available."""
    return AIOHTTP_AVAILABLE and httpx is not None


@dataclass
class _ThreadContextCache:
    """Cache entry for fetched thread context."""
    content: str
    fetched_at: float = field(default_factory=time.monotonic)
    message_count: int = 0


class SlackWebhookAdapter(BasePlatformAdapter):
    """
    Slack webhook receiver using HTTP Events API.

    Unlike the Socket Mode adapter, this receives events via HTTP POST
    to a webhook endpoint and sends all responses to a single channel.

    Required:
      - SLACK_BOT_TOKEN (xoxb-...) for API calls
      - SLACK_SIGNING_SECRET for request verification
      - CHANNEL_ID - destination channel for all messages

    Features:
      - Receives message.groups, message.im, message.mpim events
      - File/image/audio attachments
      - Thread support
      - HMAC signature validation
    """

    MAX_MESSAGE_LENGTH = 39000
    DEFAULT_PORT = 8645
    SIGNATURE_VERSION = "v0"
    MAX_SIGNATURE_AGE = 300  # 5 minutes

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.SLACK)
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._bot_user_id: Optional[str] = None
        self._channel_id: str = ""
        self._signing_secret: str = ""
        self._bot_token: str = ""
        self._http_client: Optional[httpx.AsyncClient] = None
        self._user_name_cache: Dict[str, str] = {}
        self._thread_context_cache: Dict[str, _ThreadContextCache] = {}
        self._THREAD_CACHE_TTL = 60.0

    async def connect(self) -> bool:
        """Initialize Slack webhook receiver."""
        if not AIOHTTP_AVAILABLE or httpx is None:
            logger.error("[SlackWebhook] aiohttp or httpx not installed")
            return False

        self._signing_secret = os.getenv("SLACK_SIGNING_SECRET")
        self._bot_token = os.getenv("SLACK_BOT_TOKEN")
        self._channel_id = os.getenv("CHANNEL_ID")
        self._allowed_users: set = set()

        allowed_users_env = os.getenv("SLACK_ALLOWED_USERS")
        if allowed_users_env:
            self._allowed_users = {u.strip() for u in allowed_users_env.split(",") if u.strip()}

        if not self._signing_secret:
            logger.error("[SlackWebhook] SLACK_SIGNING_SECRET not set")
            return False
        if not self._bot_token:
            logger.error("[SlackWebhook] SLACK_BOT_TOKEN not set")
            return False
        if not self._channel_id:
            logger.error("[SlackWebhook] CHANNEL_ID not set")
            return False

        # Get bot user ID for mention filtering
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {self._bot_token}"},
                )
                result = response.json()
                if result.get("ok"):
                    self._bot_user_id = result.get("user_id")
                    logger.info(
                        "[SlackWebhook] Authenticated as %s, sending to channel %s",
                        result.get("user"), self._channel_id,
                    )
                else:
                    logger.error("[SlackWebhook] auth.test failed: %s", result.get("error"))
                    return False
        except Exception as e:
            logger.error("[SlackWebhook] Failed to authenticate: %s", e)
            return False

        # Create HTTP client with bot token
        self._http_client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"Authorization": f"Bearer {self._bot_token}"},
        )

        # Setup aiohttp web server
        self._app = web.Application()
        self._app.router.add_post("/webhooks/slack", self._handle_webhook)
        self._app.router.add_get("/webhooks/slack", self._handle_health)

        port = int(self.config.extra.get("port", self.DEFAULT_PORT))
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", port)
        await site.start()

        self._running = True
        logger.info("[SlackWebhook] Listening on port %d", port)
        return True

    async def disconnect(self) -> None:
        """Disconnect and cleanup."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        self._running = False
        logger.info("[SlackWebhook] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message to the configured Slack channel."""
        if not self._http_client:
            return SendResult(success=False, error="Not connected")

        try:
            # Convert markdown to Slack mrkdwn
            formatted = self.format_message(content)

            # Build API payload
            payload = {
                "channel": self._channel_id,
                "text": formatted,
                "mrkdwn": True,
            }

            # Thread support
            thread_ts = self._resolve_thread_ts(reply_to, metadata)
            if thread_ts:
                payload["thread_ts"] = thread_ts

            # Send message
            response = await self._http_client.post(
                "https://slack.com/api/chat.postMessage",
                json=payload,
            )
            result = response.json()

            if not result.get("ok"):
                return SendResult(
                    success=False,
                    error=result.get("error", "Unknown Slack error"),
                )

            return SendResult(
                success=True,
                message_id=result.get("ts"),
                raw_response=result,
            )

        except Exception as e:
            logger.error("[SlackWebhook] Send error: %s", e, exc_info=True)
            return SendResult(success=False, error=str(e))

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image to Slack by uploading as a file."""
        if not self._http_client:
            return SendResult(success=False, error="Not connected")

        try:
            # Download image
            response = await self._http_client.get(image_url)
            response.raise_for_status()

            # Upload as file
            files_payload = {
                "channel": self._channel_id,
                "initial_comment": caption or "",
                "filename": "image.png",
            }

            thread_ts = self._resolve_thread_ts(reply_to, metadata)
            if thread_ts:
                files_payload["thread_ts"] = thread_ts

            # Use files_upload_v2 via POST with multipart
            result = await self._upload_file_content(
                content=response.content,
                filename="image.png",
                caption=caption,
                thread_ts=thread_ts,
            )

            return result

        except Exception as e:
            logger.warning("[SlackWebhook] Image upload failed: %s", e)
            # Fallback to text
            text = f"{caption}\n{image_url}" if caption else image_url
            return await self.send(chat_id, text, reply_to, metadata)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a document file to Slack."""
        if not self._http_client:
            return SendResult(success=False, error="Not connected")

        if not os.path.exists(file_path):
            return SendResult(success=False, error=f"File not found: {file_path}")

        try:
            with open(file_path, "rb") as f:
                content = f.read()

            filename = file_name or os.path.basename(file_path)
            return await self._upload_file_content(
                content=content,
                filename=filename,
                caption=caption,
                thread_ts=self._resolve_thread_ts(reply_to, metadata),
            )

        except Exception as e:
            logger.error("[SlackWebhook] Document upload failed: %s", e)
            return SendResult(success=False, error=str(e))

    async def _upload_file_content(
        self,
        content: bytes,
        filename: str,
        caption: Optional[str] = None,
        thread_ts: Optional[str] = None,
    ) -> SendResult:
        """Upload file content to Slack using files_upload_v2."""
        try:
            # Step 1: Get upload URL
            upload_payload = {
                "channel_id": self._channel_id,
                "filename": filename,
                "initial_comment": caption or "",
            }
            if thread_ts:
                upload_payload["thread_ts"] = thread_ts

            response = await self._http_client.post(
                "https://slack.com/api/files.getUploadURLExternal",
                json=upload_payload,
            )
            result = response.json()

            if not result.get("ok"):
                return SendResult(
                    success=False,
                    error=f"files.getUploadURLExternal: {result.get('error')}",
                )

            upload_url = result.get("upload_url")
            file_id = result.get("file_id")

            # Step 2: Upload the file content
            upload_response = await self._http_client.post(
                upload_url,
                headers={"Content-Type": "application/octet-stream"},
                content=content,
            )
            upload_response.raise_for_status()

            # Step 3: Complete the upload
            complete_payload = {
                "files": [{"id": file_id, "title": filename}],
            }
            if thread_ts:
                complete_payload["thread_ts"] = thread_ts

            complete_response = await self._http_client.post(
                "https://slack.com/api/files.completeUploadExternal",
                json=complete_payload,
            )
            complete_result = complete_response.json()

            if not complete_result.get("ok"):
                return SendResult(
                    success=False,
                    error=f"files.completeUploadExternal: {complete_result.get('error')}",
                )

            return SendResult(success=True, raw_response=complete_result)

        except Exception as e:
            logger.error("[SlackWebhook] File upload failed: %s", e)
            return SendResult(success=False, error=str(e))

    def _resolve_thread_ts(
        self,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Resolve the thread timestamp for replies."""
        if metadata:
            if metadata.get("thread_id"):
                return metadata["thread_id"]
            if metadata.get("thread_ts"):
                return metadata["thread_ts"]
        return reply_to

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get channel info."""
        if not self._http_client:
            return {"name": self._channel_id, "type": "channel"}

        try:
            response = await self._http_client.post(
                "https://slack.com/api/conversations.info",
                json={"channel": self._channel_id},
            )
            result = response.json()
            if result.get("ok"):
                channel = result.get("channel", {})
                return {
                    "name": channel.get("name", self._channel_id),
                    "type": "channel",
                }
        except Exception as e:
            logger.debug("[SlackWebhook] Failed to get channel info: %s", e)

        return {"name": self._channel_id, "type": "channel"}

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({
            "status": "ok",
            "platform": "slack_webhook",
            "channel_id": self._channel_id,
        })

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Handle incoming Slack webhook event."""
        # Read body
        try:
            body = await request.read()
        except Exception as e:
            logger.error("[SlackWebhook] Failed to read body: %s", e)
            return web.json_response({"error": "Bad request"}, status=400)

        # Verify signature
        if not self._verify_signature(request, body):
            logger.warning("[SlackWebhook] Invalid signature")
            return web.json_response({"error": "Invalid signature"}, status=401)

        # Parse event
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        # Handle URL verification challenge
        if payload.get("type") == "url_verification":
            challenge = payload.get("challenge", "")
            return web.Response(text=challenge, content_type="text/plain")

        # Process event callbacks
        if payload.get("type") == "event_callback":
            event = payload.get("event", {})
            event_type = event.get("type", "")
            subtype = event.get("subtype", "")

            # Handle message events
            if event_type == "message" and subtype in (None, "", "message_changed"):
                if subtype == "message_changed":
                    # Skip edits for now
                    return web.json_response({"status": "ignored"})

                await self._handle_slack_message(event)

            return web.json_response({"status": "ok"})

        return web.json_response({"status": "unknown event type"})

    def _verify_signature(self, request: web.Request, body: bytes) -> bool:
        """Verify Slack request signature."""
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")

        if not timestamp or not signature:
            return False

        # Check timestamp age
        try:
            ts = int(timestamp)
            age = abs(time.time() - ts)
            if age > self.MAX_SIGNATURE_AGE:
                logger.warning("[SlackWebhook] Request timestamp too old: %s", age)
                return False
        except ValueError:
            return False

        # Compute expected signature
        sig_basestring = f"{self.SIGNATURE_VERSION}:{timestamp}:{body.decode('utf-8')}"
        expected_sig = f"{self.SIGNATURE_VERSION}={hmac.new(self._signing_secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()}"

        return hmac.compare_digest(signature, expected_sig)

    async def _handle_slack_message(self, event: dict) -> None:
        """Process incoming Slack message event."""
        text = event.get("text", "")
        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts", "")

        # Skip bot messages (including our own)
        if event.get("bot_id"):
            return
        if user_id == self._bot_user_id:
            return

        # Skip messages from users not in the allowed list
        if self._allowed_users and user_id not in self._allowed_users:
            logger.debug("[SlackWebhook] Ignoring message from non-allowed user: %s", user_id)
            return

        # Skip message edits/deletes
        subtype = event.get("subtype", "")
        if subtype in ("message_changed", "message_deleted", "bot_message"):
            return

        # Determine message type
        msg_type = MessageType.TEXT
        if text.startswith("/"):
            msg_type = MessageType.COMMAND

        # Handle files/attachments
        media_urls = []
        media_types = []
        files = event.get("files", [])

        for f in files:
            mimetype = f.get("mimetype", "")
            url = f.get("url_private_download", "") or f.get("url_private", "")

            if not url:
                continue

            try:
                # Download file
                content = await self._download_slack_file_bytes(url)

                if mimetype.startswith("image/"):
                    ext = "." + mimetype.split("/")[-1].split(";")[0]
                    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                        ext = ".jpg"
                    cached = cache_image_from_bytes(content, ext)
                    media_urls.append(cached)
                    media_types.append(mimetype)
                    msg_type = MessageType.PHOTO

                elif mimetype.startswith("audio/"):
                    ext = "." + mimetype.split("/")[-1].split(";")[0]
                    if ext not in (".ogg", ".mp3", ".wav", ".webm", ".m4a"):
                        ext = ".ogg"
                    cached = cache_audio_from_bytes(content, ext)
                    media_urls.append(cached)
                    media_types.append(mimetype)
                    msg_type = MessageType.VOICE

                elif mimetype in SUPPORTED_DOCUMENT_TYPES.values():
                    # Reverse lookup ext from MIME
                    ext_map = {v: k for k, v in SUPPORTED_DOCUMENT_TYPES.items()}
                    ext = ext_map.get(mimetype, ".txt")
                    original_filename = f.get("name", f"document{ext}")

                    cached_path = cache_document_from_bytes(content, original_filename)
                    media_urls.append(cached_path)
                    media_types.append(mimetype)
                    msg_type = MessageType.DOCUMENT

                    # Inject text for .txt/.md
                    if ext in (".txt", ".md") and len(content) <= 100 * 1024:
                        try:
                            text_content = content.decode("utf-8")
                            injection = f"[Content of {original_filename}]:\n{text_content}\n\n"
                            text = injection + text if text else injection
                        except UnicodeDecodeError:
                            pass

            except Exception as e:
                logger.warning("[SlackWebhook] Failed to process file: %s", e)

        # Resolve user name
        user_name = await self._resolve_user_name(user_id)

        # Build source for gateway
        source = self.build_source(
            chat_id=self._channel_id,  # Always use configured channel
            chat_name=self._channel_id,
            chat_type="group",
            user_id=user_id,
            user_name=user_name,
            thread_id=thread_ts or ts,
        )

        # Fetch thread context if this is a thread reply
        if thread_ts and thread_ts != ts:
            thread_context = await self._fetch_thread_context(
                channel_id=channel_id,
                thread_ts=thread_ts,
                current_ts=ts,
            )
            if thread_context:
                text = thread_context + text

        # Create message event
        msg_event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            raw_message=event,
            message_id=ts,
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=thread_ts if thread_ts != ts else None,
        )

        # Process message through gateway
        await self.handle_message(msg_event)

    async def _resolve_user_name(self, user_id: str) -> str:
        """Resolve Slack user ID to display name."""
        if not user_id:
            return ""
        if user_id in self._user_name_cache:
            return self._user_name_cache[user_id]

        try:
            response = await self._http_client.post(
                "https://slack.com/api/users.info",
                json={"user": user_id},
            )
            result = response.json()
            if result.get("ok"):
                user = result.get("user", {})
                profile = user.get("profile", {})
                name = (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or user.get("real_name")
                    or user.get("name")
                    or user_id
                )
                self._user_name_cache[user_id] = name
                return name
        except Exception as e:
            logger.debug("[SlackWebhook] users.info failed: %s", e)

        self._user_name_cache[user_id] = user_id
        return user_id

    async def _fetch_thread_context(
        self,
        channel_id: str,
        thread_ts: str,
        current_ts: str,
        limit: int = 30,
    ) -> str:
        """Fetch thread messages for context."""
        cache_key = f"{channel_id}:{thread_ts}"
        now = time.monotonic()
        cached = self._thread_context_cache.get(cache_key)
        if cached and (now - cached.fetched_at) < self._THREAD_CACHE_TTL:
            return cached.content

        try:
            response = await self._http_client.post(
                "https://slack.com/api/conversations.replies",
                json={
                    "channel": channel_id,
                    "ts": thread_ts,
                    "limit": limit + 1,
                    "inclusive": True,
                },
            )
            result = response.json()

            if not result.get("ok"):
                return ""

            messages = result.get("messages", [])
            context_parts = []

            for msg in messages:
                msg_ts = msg.get("ts", "")
                if msg_ts == current_ts:
                    continue
                if msg.get("bot_id"):
                    continue

                msg_text = msg.get("text", "").strip()
                if not msg_text:
                    continue

                msg_user = msg.get("user", "unknown")
                name = await self._resolve_user_name(msg_user)
                is_parent = msg_ts == thread_ts
                prefix = "[thread parent] " if is_parent else ""
                context_parts.append(f"{prefix}{name}: {msg_text}")

            content = ""
            if context_parts:
                content = (
                    "[Thread context]:\n"
                    + "\n".join(context_parts)
                    + "\n[End context]\n\n"
                )

            self._thread_context_cache[cache_key] = _ThreadContextCache(
                content=content,
                fetched_at=now,
                message_count=len(context_parts),
            )
            return content

        except Exception as e:
            logger.warning("[SlackWebhook] Failed to fetch thread context: %s", e)
            return ""

    async def _download_slack_file_bytes(self, url: str) -> bytes:
        """Download a Slack file."""
        for attempt in range(3):
            try:
                response = await self._http_client.get(url)
                response.raise_for_status()
                return response.content
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise
        raise Exception("Failed to download file")

    # ----- Markdown to Slack mrkdwn -----

    def format_message(self, content: str) -> str:
        """Convert markdown to Slack mrkdwn."""
        if not content:
            return content

        placeholders = {}
        counter = [0]

        def _ph(value: str) -> str:
            key = f"\x00SL{counter[0]}\x00"
            counter[0] += 1
            placeholders[key] = value
            return key

        text = content

        # Protect code blocks
        text = re.sub(r'(```(?:[^\n]*\n)?[\s\S]*?```)', lambda m: _ph(m.group(0)), text)
        text = re.sub(r'(`[^`]+`)', lambda m: _ph(m.group(0)), text)

        # Convert links
        def _convert_link(m):
            label = m.group(1)
            url = m.group(2).strip()
            if url.startswith('<') and url.endswith('>'):
                url = url[1:-1].strip()
            return _ph(f'<{url}|{label}>')

        text = re.sub(r'\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)', _convert_link, text)

        # Protect Slack entities
        text = re.sub(r'(<(?:[@#!]|(?:https?|mailto|tel):)[^>\n]+>)', lambda m: _ph(m.group(1)), text)

        # Escape special chars
        text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        # Headers
        def _convert_header(m):
            inner = m.group(1).strip()
            inner = re.sub(r'\*\*(.+?)\*\*', r'\1', inner)
            return _ph(f'*{inner}*')

        text = re.sub(r'^#{1,6}\s+(.+)$', _convert_header, text, flags=re.MULTILINE)

        # Bold/italic
        text = re.sub(r'\*\*\*(.+?)\*\*\*', lambda m: _ph(f'*_{m.group(1)}_*'), text)
        text = re.sub(r'\*\*(.+?)\*\*', lambda m: _ph(f'*{m.group(1)}*'), text)
        text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', lambda m: _ph(f'_{m.group(1)}_'), text)
        text = re.sub(r'~~(.+?)~~', lambda m: _ph(f'~{m.group(1)}~'), text)

        # Restore placeholders
        for key in reversed(placeholders):
            text = text.replace(key, placeholders[key])

        return text
