"""
Cached, database-backed sessions.
"""

import logging

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore as DBStore
from django.core.cache import caches

KEY_PREFIX = "django.contrib.sessions.cached_db"

logger = logging.getLogger("django.contrib.sessions")


class SessionStore(DBStore):
    """
    Implement cached, database backed sessions.
    """

    cache_key_prefix = KEY_PREFIX
    invalidated_cache_key_prefix = KEY_PREFIX + ".invalidated"

    def __init__(self, session_key=None):
        self._cache = caches[settings.SESSION_CACHE_ALIAS]
        super().__init__(session_key)

    @property
    def cache_key(self):
        return self.cache_key_prefix + self._get_or_create_session_key()

    async def acache_key(self):
        return self.cache_key_prefix + await self._aget_or_create_session_key()

    def _is_invalidated(self, session_key):
        """
        Return True if the given session key carries a tombstone marker left
        by cycle_key() when the stale cache entry couldn't be deleted.
        """
        try:
            return bool(
                self._cache.get(self.invalidated_cache_key_prefix + session_key)
            )
        except Exception:
            return False

    async def _ais_invalidated(self, session_key):
        """See _is_invalidated()."""
        try:
            return bool(
                await self._cache.aget(self.invalidated_cache_key_prefix + session_key)
            )
        except Exception:
            return False

    def load(self):
        data = None
        if not self._is_invalidated(self.session_key):
            try:
                data = self._cache.get(self.cache_key)
            except Exception:
                # Some backends (e.g. memcache) raise an exception on invalid
                # cache keys. If this happens, reset the session. See #17810.
                data = None

        if data is None:
            s = self._get_session_from_db()
            if s:
                data = self.decode(s.session_data)
                self._cache.set(
                    self.cache_key, data, self.get_expiry_age(expiry=s.expire_date)
                )
            else:
                data = {}
        return data

    async def aload(self):
        data = None
        if not await self._ais_invalidated(self.session_key):
            try:
                data = await self._cache.aget(await self.acache_key())
            except Exception:
                # Some backends (e.g. memcache) raise an exception on invalid
                # cache keys. If this happens, reset the session. See #17810.
                data = None

        if data is None:
            s = await self._aget_session_from_db()
            if s:
                data = self.decode(s.session_data)
                await self._cache.aset(
                    await self.acache_key(),
                    data,
                    await self.aget_expiry_age(expiry=s.expire_date),
                )
            else:
                data = {}
        return data

    def exists(self, session_key):
        return (
            session_key
            and not self._is_invalidated(session_key)
            and (self.cache_key_prefix + session_key) in self._cache
            or super().exists(session_key)
        )

    async def aexists(self, session_key):
        return (
            session_key
            and not await self._ais_invalidated(session_key)
            and (self.cache_key_prefix + session_key) in self._cache
            or await super().aexists(session_key)
        )

    def cycle_key(self):
        """
        Create a new session key, while retaining the current session data.

        The database records are rotated atomically (see
        db.SessionStore.cycle_key()). Once that transaction commits, the old
        cache entry is invalidated so the old key can't be resurrected from
        a stale cache. A cache failure never rolls back the committed
        database rotation; instead, the read path cooperates with a
        tombstone marker (see _invalidate_cache_entry()).
        """
        key = self.session_key
        if key is not None:
            # Capture the expiry before rotation so a tombstone, if needed,
            # outlives the stale cache entry.
            expiry_age = self.get_expiry_age()
        super().cycle_key()
        if key is not None:
            self._invalidate_cache_entry(key, expiry_age)

    async def acycle_key(self):
        """See cycle_key()."""
        key = self.session_key
        if key is not None:
            expiry_age = await self.aget_expiry_age()
        await super().acycle_key()
        if key is not None:
            await self._ainvalidate_cache_entry(key, expiry_age)

    def _invalidate_cache_entry(self, session_key, expiry_age):
        """
        Invalidate the cache entry of a rotated-out session key.

        Deleting the entry is attempted first. If that fails, a tombstone
        marker is written instead and load()/exists() ignore the stale
        entry, falling back to the database where the key is already gone.
        This is best-effort coordination between two stores, not a
        cross-store atomic guarantee: if the tombstone can't be written
        either, the stale entry may remain readable until it expires.
        """
        try:
            self._cache.delete(self.cache_key_prefix + session_key)
            return
        except Exception:
            logger.exception("Error deleting from cache (%s)", self._cache)
        try:
            self._cache.set(
                self.invalidated_cache_key_prefix + session_key, True, expiry_age
            )
        except Exception:
            logger.exception("Error invalidating cache (%s)", self._cache)

    async def _ainvalidate_cache_entry(self, session_key, expiry_age):
        """See _invalidate_cache_entry()."""
        try:
            await self._cache.adelete(self.cache_key_prefix + session_key)
            return
        except Exception:
            logger.exception("Error deleting from cache (%s)", self._cache)
        try:
            await self._cache.aset(
                self.invalidated_cache_key_prefix + session_key, True, expiry_age
            )
        except Exception:
            logger.exception("Error invalidating cache (%s)", self._cache)

    def save(self, must_create=False):
        super().save(must_create)
        try:
            self._cache.set(self.cache_key, self._session, self.get_expiry_age())
        except Exception:
            logger.exception("Error saving to cache (%s)", self._cache)

    async def asave(self, must_create=False):
        await super().asave(must_create)
        try:
            await self._cache.aset(
                await self.acache_key(),
                self._session,
                await self.aget_expiry_age(),
            )
        except Exception:
            logger.exception("Error saving to cache (%s)", self._cache)

    def delete(self, session_key=None):
        super().delete(session_key)
        if session_key is None:
            if self.session_key is None:
                return
            session_key = self.session_key
        try:
            self._cache.delete(self.cache_key_prefix + session_key)
        except Exception:
            logger.exception("Error deleting from cache (%s)", self._cache)

    async def adelete(self, session_key=None):
        await super().adelete(session_key)
        if session_key is None:
            if self.session_key is None:
                return
            session_key = self.session_key
        try:
            await self._cache.adelete(self.cache_key_prefix + session_key)
        except Exception:
            logger.exception("Error deleting from cache (%s)", self._cache)

    def flush(self):
        """
        Remove the current session data from the database and regenerate the
        key.
        """
        self.clear()
        self.delete(self.session_key)
        self._session_key = None

    async def aflush(self):
        """See flush()."""
        self.clear()
        await self.adelete(self.session_key)
        self._session_key = None
