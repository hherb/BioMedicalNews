"""Matrix delivery: one authenticated HTTP PUT, no SDK.

The client-server API is simple enough that a `matrix-nio` dependency would buy
nothing here — sending a room message is a single request, and ``httpx`` is
already a dependency. What the SDK *would* buy is end-to-end encryption, and
that is a deliberate non-requirement: see :meth:`MatrixChannel._refuse_if_encrypted`.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from bmnews.constants import HTTP_TIMEOUT_SECONDS
from bmnews.notify.channels import ChannelError, Message

logger = logging.getLogger(__name__)

#: Client-server API version this adapter speaks.
_API = "/_matrix/client/v3"


class MatrixChannel:
    """Posts a notification into an unencrypted Matrix room."""

    def __init__(
        self,
        *,
        name: str,
        homeserver: str,
        access_token: str,
        room: str,
        client: Any = None,
    ) -> None:
        """Configure the adapter.

        Args:
            name: The channel's config name, used in errors and log messages.
            homeserver: Base URL of the homeserver, e.g.
                ``https://matrix.example.org``.
            access_token: Bearer token for the sending account.
            room: Room id (``!abc:server``) or alias (``#alerts:server``).
            client: An ``httpx.Client``-shaped object. Injected by tests; built
                on first use otherwise.
        """
        self.name = name
        self._homeserver = homeserver.rstrip("/")
        self._access_token = access_token
        self._room = room
        self._client = client
        self._room_id: str = ""

    def send(self, message: Message, *, txn_key: str) -> None:
        """Post *message* to the room.

        Args:
            message: The rendered notification. ``text`` becomes the required
                plain ``body`` and ``html`` the ``formatted_body`` beside it.
            txn_key: The transaction id, which the homeserver treats as an
                idempotency key — a repeat PUT carrying one it has already seen
                returns the original event instead of posting again. Derive it
                with :func:`bmnews.notify.channels.transaction_key`, never
                randomly, or a retry after a crash duplicates the message.

        Raises:
            ChannelError: If the room is encrypted, an alias cannot be
                resolved, or the homeserver rejects the request.
        """
        room_id = self._resolve_room()
        self._refuse_if_encrypted(room_id)

        url = f"{self._url(room_id)}/send/m.room.message/{quote(txn_key, safe='')}"
        response = self._http().put(
            url,
            headers=self._headers(),
            json={
                "msgtype": "m.text",
                "body": message.text,
                "format": "org.matrix.custom.html",
                "formatted_body": message.html,
            },
        )

        if response.status_code != 200:
            raise ChannelError(
                f"channel {self.name!r} got HTTP {response.status_code} from the homeserver: "
                f"{_error_detail(response)}"
            )

        logger.info("Notification posted to Matrix room %s over channel %r", room_id, self.name)

    # --- Room resolution ----------------------------------------------------

    def _resolve_room(self) -> str:
        """Return the room id, resolving and caching an alias once.

        Aliases are what humans actually have written down, but the send
        endpoint takes an id. Resolution is cached for the adapter's lifetime —
        an alias points at the same room for as long as a run lasts, and
        re-resolving it per batch would be a request per delivery.
        """
        if self._room_id:
            return self._room_id

        if not self._room.startswith("#"):
            self._room_id = self._room
            return self._room_id

        url = f"{self._homeserver}{_API}/directory/room/{quote(self._room, safe='')}"
        response = self._http().get(url, headers=self._headers())
        if response.status_code != 200:
            raise ChannelError(
                f"channel {self.name!r} could not resolve room alias {self._room!r}: "
                f"HTTP {response.status_code} {_error_detail(response)}"
            )

        room_id = str(response.json().get("room_id", "")).strip()
        if not room_id:
            raise ChannelError(
                f"channel {self.name!r}: homeserver returned no room_id for {self._room!r}"
            )

        self._room_id = room_id
        return room_id

    def _refuse_if_encrypted(self, room_id: str) -> None:
        """Fail rather than post ciphertext nobody in the room can read.

        A plain HTTP PUT cannot produce a readable message in an end-to-end
        encrypted room — that needs megolm, i.e. ``matrix-nio`` and ``libolm``.
        Unencrypted rooms are the supported configuration indefinitely rather
        than a first-version shortcut: the content is alerts about public
        preprints, so there is nothing confidential to protect.

        The check exists because the failure is otherwise invisible. The PUT
        succeeds, the notification is recorded as delivered, and the room shows
        an undecryptable message.

        A homeserver that will not answer the state query at all is not treated
        as a refusal: the send that follows reports the real error, and
        guessing here would block delivery to rooms that are perfectly fine.
        """
        url = f"{self._url(room_id)}/state/m.room.encryption"
        response = self._http().get(url, headers=self._headers())

        if response.status_code == 200:
            raise ChannelError(
                f"channel {self.name!r}: room {room_id} is encrypted, and this adapter cannot "
                "post readable messages there — use an unencrypted room"
            )
        if response.status_code != 404:
            logger.debug(
                "Could not read encryption state of %s (HTTP %s); continuing",
                room_id,
                response.status_code,
            )

    # --- Plumbing -----------------------------------------------------------

    def _url(self, room_id: str) -> str:
        """Base URL for room-scoped endpoints."""
        return f"{self._homeserver}{_API}/rooms/{quote(room_id, safe='')}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
        return self._client


def _error_detail(response: Any) -> str:
    """Pull the human-readable part out of a Matrix error response."""
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body is still worth reporting
        return getattr(response, "text", "")
    if isinstance(payload, dict):
        return str(payload.get("error") or payload.get("errcode") or payload)
    return str(payload)
