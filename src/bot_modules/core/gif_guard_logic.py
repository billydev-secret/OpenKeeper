"""GIF Guard — which channels are guarded, and what counts as a GIF there.

Pure predicates over the pieces of a message the gate reads; the delete itself
lives beside the spoiler gate in ``core/post_monitoring.enforce_gif_guard``.
Nothing about a message is recorded — a GIF is recognised by filename, by
Discord's own animated flag, by link host, or by the kind of preview Discord
built for it.

Every rule below makes something deletable, so each is deliberately narrow:
a still ``.webp`` stays, a link suppressed with ``<...>`` (which shows no
preview) stays, and ``tenor``/``giphy`` only count as the host of a real link,
never as a word in a sentence.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

#: Hosts whose every link is a GIF as Discord shows it. Subdomains count
#: (``media.tenor.com``, ``i.giphy.com``).
GIF_HOSTS = ("tenor.com", "giphy.com", "gph.is")

#: A link ending in one of these is a GIF whatever the host — Imgur's
#: ``i.imgur.com/x.gifv`` and any raw ``.gif`` hotlink.
GIF_PATH_SUFFIXES = (".gif", ".gifv")

#: Upload filenames that are always GIFs. An animated ``.webp`` is caught by
#: Discord's own animated flag instead, so a still ``.webp`` is left alone.
GIF_FILE_EXTENSIONS = (".gif",)

# A bare http(s) link. The negative lookbehind skips ``<https://…>``, the
# Discord syntax for posting a link without a preview — nothing animates, so
# there is nothing to guard.
_URL_RE = re.compile(r"(?<!<)https?://[^\s<>|]+", re.IGNORECASE)


def is_guarded_channel(
    channel_id: int, parent_id: int | None, guarded_ids: frozenset[int] | set[int]
) -> bool:
    """True when the channel, or the channel a thread hangs off, is guarded.

    An empty set guards nothing — that is the off state, and why the feature
    ships dark.
    """
    if not guarded_ids:
        return False
    return channel_id in guarded_ids or (
        parent_id is not None and parent_id in guarded_ids
    )


def is_gif_url(url: str) -> bool:
    """A link Discord would show as a GIF: a GIF host, or a ``.gif``/``.gifv`` path."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in GIF_HOSTS):
        return True
    return parts.path.lower().endswith(GIF_PATH_SUFFIXES)


def content_has_gif_link(content: str | None) -> bool:
    """True when the message text carries a previewed GIF link.

    Discord's GIF picker posts the Tenor link as the message text, so this is
    what catches a picker GIF the moment it arrives, before any preview exists.
    """
    if not content:
        return False
    return any(is_gif_url(m.group(0)) for m in _URL_RE.finditer(content))


def attachment_is_gif(
    filename: str, content_type: str | None, animated: bool
) -> bool:
    """An uploaded GIF: a ``.gif`` file, an ``image/gif`` upload, or any file
    Discord itself flags as animated (an animated ``.webp``)."""
    if animated:
        return True
    if content_type and content_type.lower().split(";")[0].strip() == "image/gif":
        return True
    return filename.lower().endswith(GIF_FILE_EXTENSIONS)


def embed_is_gif(embed_type: str | None, url: str | None) -> bool:
    """A preview Discord built as a GIF.

    ``gifv`` is the type Discord gives Tenor, Giphy and Imgur animations; an
    ``image`` preview of a ``.gif`` link is the other shape. This is the
    backstop for GIF links from hosts :data:`GIF_HOSTS` doesn't name — their
    preview only arrives in a later edit, after the message itself.
    """
    if embed_type == "gifv":
        return True
    return embed_type == "image" and bool(url) and is_gif_url(url or "")


def message_has_gif(
    content: str | None,
    attachments: Iterable[tuple[str, str | None, bool]],
    embeds: Iterable[tuple[str | None, str | None]],
) -> bool:
    """Whether any part of a message is a GIF.

    ``attachments`` are ``(filename, content_type, animated)`` and ``embeds``
    are ``(type, url)`` — plain values, so this is testable without Discord.
    """
    if content_has_gif_link(content):
        return True
    if any(attachment_is_gif(*a) for a in attachments):
        return True
    return any(embed_is_gif(*e) for e in embeds)
