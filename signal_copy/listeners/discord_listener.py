"""
Discord signal-channel listener.

Reads messages from configured Discord channels and forwards their text to a
callback (the orchestrator). Supports two backends:

1. discord.py bot account: bot token + the channels it can see (needs the
   privileged message-content intent enabled in the dev portal).
2. Self-bot via discord.py-self (user token). READ-ONLY here. NOTE: using a
   user token violates Discord ToS (ban risk) — the user accepted this.

Why polling? discord.py-self does NOT reliably stream MESSAGE_CREATE for every
guild on accounts that are members of many servers (gateway "lazy guild"
subscriptions). To guarantee we never miss a signal in a watched channel, this
listener ALSO polls each configured channel's recent history over REST on an
interval. Gateway + polling are de-duplicated by message id.

Configure via signal_copy/signal_copy_config.py:
  DISCORD_BOT_TOKEN / DISCORD_USER_TOKEN, DISCORD_CHANNELS (id allowlist),
  DISCORD_GUILDS (watched guilds for diagnostics + subscription),
  DISCORD_POLL_INTERVAL, DISCORD_POLL_ENABLED.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Awaitable, Callable, List, Optional

from utils.logger import logger

MessageCallback = Callable[[str, str, Optional[int]], Awaitable[None]]


class DiscordSignalListener:
    def __init__(
        self,
        on_message: MessageCallback,
        *,
        bot_token: str = "",
        channels: Optional[List[int]] = None,
        channel_names: Optional[dict] = None,
        selfbot: bool = False,
        debug_guilds: Optional[List[int]] = None,
        poll_enabled: Optional[bool] = None,
        poll_interval: float = 25.0,
    ):
        self.on_message = on_message
        self.bot_token = bot_token or ""
        self.channels = set(channels or [])
        self.channel_names = channel_names or {}
        self.selfbot = bool(selfbot)
        self.debug_guilds = set(debug_guilds or [])
        # Default: poll only in selfbot mode (where gateway is unreliable).
        self.poll_enabled = self.selfbot if poll_enabled is None else bool(poll_enabled)
        self.poll_interval = max(8.0, float(poll_interval))
        self.running = False
        self._client = None
        # De-dup of already-forwarded message ids (bounded ring buffer).
        self._seen_ids: "deque[int]" = deque(maxlen=4000)
        self._seen_set: set[int] = set()

    # ---- helpers -----------------------------------------------------------
    def _mark_seen(self, mid: Optional[int]) -> bool:
        """Return True if this message id is new (and record it)."""
        if mid is None:
            return True
        if mid in self._seen_set:
            return False
        if len(self._seen_ids) == self._seen_ids.maxlen:
            old = self._seen_ids[0]
            self._seen_set.discard(old)
        self._seen_ids.append(mid)
        self._seen_set.add(mid)
        return True

    @staticmethod
    def _embed_text(embeds) -> str:
        parts = []
        for e in embeds or []:
            if getattr(e, "title", None):
                parts.append(str(e.title))
            if getattr(e, "description", None):
                parts.append(str(e.description))
            for fld in getattr(e, "fields", []) or []:
                parts.append(f"{getattr(fld, 'name', '')}: {getattr(fld, 'value', '')}")
        return "\n".join(p for p in parts if p)

    @classmethod
    def _extract_text(cls, message) -> str:
        parts = []
        msg = message
        has_snaps = bool(getattr(msg, "message_snapshots", None))
        own = getattr(msg, "content", "") or ""
        if own and own.strip():
            parts.append(own)
        et = cls._embed_text(getattr(message, "embeds", None))
        if et:
            parts.append(et)
        if not own and has_snaps:
            for snap in (getattr(msg, "message_snapshots", None) or []):
                sc = getattr(snap, "content", "") or ""
                if sc:
                    parts.append(sc)
                se = cls._embed_text(getattr(snap, "embeds", None))
                if se:
                    parts.append(se)
        txt = "\n".join(p for p in parts if p and p.strip())
        return txt

    def _channel_label(self, ch, cid) -> str:
        return self.channel_names.get(cid, getattr(ch, "name", "") or str(cid))

    async def _fetch_url(self, url: str) -> Optional[bytes]:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(url)
                if r.status_code == 200 and r.content:
                    return r.content
        except Exception:
            pass
        return None

    async def _extract_image(self, message) -> Optional[bytes]:
        """Return bytes of the first chart image (attachment or embed, including
        forwarded message_snapshots), or None."""
        try:
            sources = [message] + list(getattr(message, "message_snapshots", None) or [])
            # 1) attachments
            for src in sources:
                for att in getattr(src, "attachments", []) or []:
                    fn = (getattr(att, "filename", "") or "").lower()
                    ct = (getattr(att, "content_type", "") or "").lower()
                    if ct.startswith("image/") or fn.endswith((".png", ".jpg", ".jpeg", ".webp")):
                        try:
                            return await att.read()
                        except Exception:
                            u = getattr(att, "url", None)
                            if u:
                                return await self._fetch_url(u)
            # 2) embed image / thumbnail urls
            for src in sources:
                for e in getattr(src, "embeds", []) or []:
                    img = getattr(e, "image", None)
                    thumb = getattr(e, "thumbnail", None)
                    u = (getattr(img, "url", None) if img else None) \
                        or (getattr(thumb, "url", None) if thumb else None)
                    if u:
                        data = await self._fetch_url(u)
                        if data:
                            return data
        except Exception as exc:
            logger.debug("[DISCORD_LISTENER] image extract failed: %s", exc)
        return None

    async def _forward(self, message, *, via: str) -> None:
        """Validate match + de-dup + forward a message to the orchestrator."""
        client = self._client
        # In BOT mode, skip our own messages (the bot posts). In SELFBOT mode the
        # client IS the user's own account and never posts — so we MUST NOT skip
        # "own" messages, otherwise the user's own forwards/posts are ignored.
        if (not self.selfbot) and client is not None and getattr(client, "user", None) is not None:
            if getattr(message, "author", None) == client.user:
                return  # never react to our own messages

        ch = message.channel
        cid = getattr(ch, "id", None)
        parent_id = getattr(ch, "parent_id", None)      # threads/forum posts
        category_id = getattr(ch, "category_id", None)
        guild_id = getattr(getattr(message, "guild", None), "id", None)

        matched = bool(self.channels) and (
            cid in self.channels
            or (parent_id is not None and parent_id in self.channels)
            or (category_id is not None and category_id in self.channels)
        )
        if self.channels and not matched:
            if guild_id in self.debug_guilds:
                logger.info(
                    "[DISCORD_RX_UNMATCHED] guild=%s ch=%s parent=%s cat=%s type=%s :: %s",
                    guild_id, cid, parent_id, category_id, type(ch).__name__,
                    (message.content or "")[:60].replace("\n", " "),
                )
            return

        if not self._mark_seen(getattr(message, "id", None)):
            return  # already handled via the other path (gateway/poll)

        text = self._extract_text(message)
        if not text.strip():
            if guild_id in self.debug_guilds:
                snaps = getattr(message, "message_snapshots", None) or []
                logger.info("[DISCORD_RX_EMPTY] guild=%s ch=%s flags=%s ref=%s snaps=%s "
                            "attach=%s embeds=%s snap_attrs=%s",
                            guild_id, cid, getattr(message, "flags", None),
                            getattr(getattr(message, "reference", None), "type", None),
                            len(snaps), len(getattr(message, "attachments", None) or []),
                            len(getattr(message, "embeds", None) or []),
                            [a for a in dir(message) if "snap" in a.lower()])
            return
        name = self._channel_label(ch, cid)
        image = await self._extract_image(message)
        logger.info("[DISCORD_RX:%s] guild=%s ch=%s img=%s :: %s", via, guild_id, cid,
                    bool(image), text[:60].replace("\n", " "))
        await self.on_message(text, f"discord:{name}", cid, image)

    # ---- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        try:
            import discord
        except Exception:
            logger.error("[DISCORD_LISTENER] discord library not installed; listener disabled "
                         "(bot mode: pip install discord.py | selfbot mode: pip install discord.py-self)")
            return
        if not self.bot_token:
            logger.error("[DISCORD_LISTENER] no token; listener disabled")
            return

        if self.selfbot:
            try:
                client = discord.Client()
            except Exception as exc:
                logger.error("[DISCORD_LISTENER] selfbot client init failed (need discord.py-self): %s", exc)
                return
            mode = "SELFBOT(user-token, READ-ONLY)"
        else:
            intents = discord.Intents.default()
            intents.message_content = True  # privileged intent: enable in dev portal
            client = discord.Client(intents=intents)
            mode = "BOT"
        self._client = client

        @client.event
        async def on_ready():
            self.running = True
            logger.info("[DISCORD_LISTENER] connected as %s | mode=%s | channels=%s | poll=%s/%.0fs",
                        getattr(client, "user", "?"), mode, self.channels or "ALL visible",
                        self.poll_enabled, self.poll_interval)
            # Enumerate guilds so we can confirm membership + discover real ids,
            # and explicitly subscribe to watched guilds (fixes lazy-loading).
            try:
                guilds = list(getattr(client, "guilds", []) or [])
                logger.info("[DISCORD_GUILDS] member of %d guild(s):", len(guilds))
                for g in guilds:
                    flag = " <== WATCHED" if g.id in self.debug_guilds else ""
                    logger.info("[DISCORD_GUILDS]   guild=%s name=%r%s", g.id,
                                getattr(g, "name", "?"), flag)
                for g in guilds:
                    if g.id not in self.debug_guilds:
                        continue
                    try:
                        for ch in getattr(g, "text_channels", []) or []:
                            logger.info("[DISCORD_GUILD_CH] guild=%s ch=%s name=%r cat=%s",
                                        g.id, ch.id, getattr(ch, "name", "?"),
                                        getattr(ch, "category_id", None))
                    except Exception as exc:
                        logger.warning("[DISCORD_GUILDS] cannot list channels for %s: %s", g.id, exc)
                    try:
                        sub = getattr(g, "subscribe", None)
                        if callable(sub):
                            await g.subscribe()
                            logger.info("[DISCORD_GUILDS] subscribed to guild=%s", g.id)
                    except Exception as exc:
                        logger.warning("[DISCORD_GUILDS] subscribe failed for %s: %s", g.id, exc)
            except Exception as exc:
                logger.warning("[DISCORD_GUILDS] enumeration error: %s", exc)

            # Start REST polling fallback for configured channels.
            if self.poll_enabled and self.channels:
                client.loop.create_task(self._poll_loop())

        @client.event
        async def on_message(message):
            try:
                await self._forward(message, via="gw")
            except Exception as exc:
                logger.exception("[DISCORD_LISTENER] handler error: %s", exc)

        logger.info("[DISCORD_LISTENER] starting (%s)…", mode)
        await client.start(self.bot_token)

    async def _poll_loop(self) -> None:
        """Periodically fetch recent history of each watched channel over REST.

        Messages OLDER than the baseline (listener start minus a short grace)
        are primed (marked seen, not replayed) so we never spam old history.
        Messages at/after the baseline are forwarded once — this survives bot
        restarts (a signal forwarded shortly before/after a restart is still
        processed), unlike a blanket first-sweep prime.
        """
        client = self._client
        if client is None:
            return
        import datetime as _dt
        import discord  # local import; library is present if we got here

        baseline = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=180)
        logger.info("[DISCORD_POLL] starting; channels=%s interval=%.0fs baseline=%s",
                    self.channels, self.poll_interval, baseline.isoformat(timespec="seconds"))
        while True:
            try:
                if getattr(client, "is_closed", lambda: False)():
                    return
                for cid in list(self.channels):
                    try:
                        ch = client.get_channel(cid)
                        if ch is None:
                            ch = await client.fetch_channel(cid)
                        history = getattr(ch, "history", None)
                        if history is None:
                            continue  # category/forum container: nothing to poll
                        msgs = [m async for m in ch.history(limit=8)]
                        msgs.reverse()  # oldest -> newest
                        for m in msgs:
                            created = getattr(m, "created_at", None)
                            if created is not None and created < baseline:
                                # old message: prime so it never replays
                                self._mark_seen(getattr(m, "id", None))
                            else:
                                # recent/new: _forward de-dups via _mark_seen
                                await self._forward(m, via="poll")
                    except discord.Forbidden:
                        logger.warning("[DISCORD_POLL] no access to channel %s", cid)
                    except discord.NotFound:
                        logger.warning("[DISCORD_POLL] channel %s not found", cid)
                    except Exception as exc:
                        logger.warning("[DISCORD_POLL] error polling %s: %s: %r",
                                       cid, type(exc).__name__, exc)
            except Exception as exc:
                logger.warning("[DISCORD_POLL] loop error: %s", exc)
            await asyncio.sleep(self.poll_interval)

    async def stop(self) -> None:
        self.running = False
        try:
            if self._client:
                await self._client.close()
        except Exception:
            pass
