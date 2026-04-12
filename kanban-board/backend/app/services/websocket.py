"""WebSocket connection manager for real-time board event broadcasting."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    """Manages WebSocket connections grouped by board ID.

    Clients connect to a specific board room and receive JSON-encoded
    events whenever cards are created, updated, deleted, or moved.
    """

    def __init__(self) -> None:
        """Initialise the connection manager with an empty room map."""
        self._rooms: dict[int, list[WebSocket]] = defaultdict(list)

    async def connect(self, board_id: int, websocket: WebSocket) -> None:
        """Accept and register a WebSocket connection to a board room.

        Args:
            board_id: The board to subscribe to.
            websocket: The incoming WebSocket connection.
        """
        await websocket.accept()
        self._rooms[board_id].append(websocket)

    def disconnect(self, board_id: int, websocket: WebSocket) -> None:
        """Remove a WebSocket connection from a board room.

        Args:
            board_id: The board the client was subscribed to.
            websocket: The WebSocket connection to remove.
        """
        connections = self._rooms.get(board_id, [])
        if websocket in connections:
            connections.remove(websocket)
        if not connections and board_id in self._rooms:
            del self._rooms[board_id]

    async def broadcast(self, board_id: int, event: dict[str, Any]) -> None:
        """Send a JSON event to all clients connected to a board room.

        Stale connections that raise an exception are silently removed.

        Args:
            board_id: The board room to broadcast to.
            event: The event payload to send as JSON.
        """
        connections = list(self._rooms.get(board_id, []))
        message = json.dumps(event)
        stale: list[WebSocket] = []
        for ws in connections:
            try:
                await ws.send_text(message)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(board_id, ws)

    def active_connections(self, board_id: int) -> int:
        """Return the number of active connections for a board.

        Args:
            board_id: The board to query.

        Returns:
            The count of currently connected WebSocket clients.
        """
        return len(self._rooms.get(board_id, []))


# Singleton instance used across the application
manager = ConnectionManager()
