"""WebSocket endpoint for real-time board event streaming."""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jose import JWTError

from app.services.auth import decode_token
from app.services.websocket import manager

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/boards/{board_id}")
async def board_websocket(websocket: WebSocket, board_id: int) -> None:
    """WebSocket endpoint for receiving real-time board events.

    Clients connect with an optional ``token`` query parameter containing a
    valid JWT access token.  Once connected, the server pushes JSON-encoded
    events such as ``card_created``, ``card_updated``, ``card_deleted``,
    ``card_moved``, and ``column_reordered`` to all clients subscribed to
    the given board.

    The connection stays open until the client disconnects or an error occurs.

    Args:
        websocket: The incoming WebSocket connection.
        board_id: The board to subscribe to for real-time events.
    """
    # Optional token validation — accept connection even without a valid
    # token for development convenience, but validate if provided.
    token = websocket.query_params.get("token")
    if token:
        try:
            decode_token(token)
        except (JWTError, Exception):
            await websocket.close(code=4001, reason="Invalid token")
            return

    await manager.connect(board_id, websocket)
    try:
        # Keep the connection alive, listening for client messages.
        # The server is primarily a broadcaster — client messages are
        # acknowledged but not processed.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(board_id, websocket)
    except Exception:
        manager.disconnect(board_id, websocket)
