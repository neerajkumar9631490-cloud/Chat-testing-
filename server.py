import eventlet
eventlet.monkey_patch()

from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import os
import time
import random
import string

# ─────────────────────────────────────────
#  App setup
# ─────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "darkweb-chat-secret-2024")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
)

# ─────────────────────────────────────────
#  In-memory state
#
#  connected_users = {
#      sid: {
#          "username": str,
#          "room":     str,   # "global" | room_code
#          "joined_at": int,
#      }
#  }
#
#  rooms = {
#      room_code: {
#          "users": set(sid, ...),
#          "type":  "global" | "private",
#      }
#  }
#
#  typing_users = {
#      room_code: {
#          sid: username,
#      }
#  }
# ─────────────────────────────────────────
connected_users: dict = {}
rooms: dict = {
    "global": {
        "users": set(),
        "type": "global",
    }
}
typing_users: dict = {
    "global": {}
}

GLOBAL_ROOM = "global"
ROOM_CODE_LENGTH = 6
ROOM_CODE_CHARS = string.ascii_uppercase + string.digits


# ─────────────────────────────────────────
#  Static route
# ─────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


# ─────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────
def timestamp() -> int:
    """Current time in milliseconds."""
    return int(time.time() * 1000)


def generate_room_code() -> str:
    """Generate a unique 6-character uppercase alphanumeric room code."""
    while True:
        code = "".join(random.choices(ROOM_CODE_CHARS, k=ROOM_CODE_LENGTH))
        if code not in rooms:
            return code


def ensure_room(room_code: str, room_type: str = "private") -> None:
    """Create room tracking structures if they don't already exist."""
    if room_code not in rooms:
        rooms[room_code] = {"users": set(), "type": room_type}
    if room_code not in typing_users:
        typing_users[room_code] = {}


def cleanup_room(room_code: str) -> None:
    """Delete a private room if it has become empty."""
    if room_code == GLOBAL_ROOM:
        return
    if room_code in rooms and not rooms[room_code]["users"]:
        rooms.pop(room_code, None)
        typing_users.pop(room_code, None)
        print(f"[~] Private room {room_code} deleted (empty)")


def broadcast_user_list(room_code: str) -> None:
    """Emit the current user list to everyone in the given room."""
    if room_code not in rooms:
        return
    room_sids = rooms[room_code]["users"]
    users = [
        connected_users[sid]["username"]
        for sid in room_sids
        if sid in connected_users
    ]
    socketio.emit(
        "user_list",
        {"users": users, "count": len(users)},
        room=room_code,
    )


def broadcast_typing(room_code: str, exclude_sid: str) -> None:
    """Emit typing_update to everyone in the room except the typer."""
    if room_code not in typing_users:
        return
    others = [
        username
        for sid, username in typing_users[room_code].items()
        if sid != exclude_sid
    ]
    socketio.emit("typing_update", {"users": others}, room=room_code)


def place_user_in_room(sid: str, room_code: str) -> None:
    """
    Move a socket into a Flask-SocketIO room and update tracking.
    If the user was previously in a different room, leave it first.
    """
    user = connected_users.get(sid)
    if not user:
        return

    old_room = user.get("room")

    # Leave old room cleanly
    if old_room and old_room != room_code:
        leave_room(old_room)
        if old_room in rooms:
            rooms[old_room]["users"].discard(sid)
        if old_room in typing_users:
            typing_users[old_room].pop(sid, None)
        broadcast_typing(old_room, sid)
        broadcast_user_list(old_room)
        cleanup_room(old_room)

    # Ensure new room exists
    ensure_room(room_code, room_type="global" if room_code == GLOBAL_ROOM else "private")

    # Join new room
    join_room(room_code)
    rooms[room_code]["users"].add(sid)
    connected_users[sid]["room"] = room_code


# ─────────────────────────────────────────
#  Connection lifecycle
# ─────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    print(f"[+] Socket connected: {request.sid}")


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid

    if sid not in connected_users:
        print(f"[-] Unknown socket disconnected: {sid}")
        return

    user = connected_users[sid]
    username = user["username"]
    room_code = user.get("room", GLOBAL_ROOM)

    # Clean up tracking
    connected_users.pop(sid, None)

    if room_code in rooms:
        rooms[room_code]["users"].discard(sid)
    if room_code in typing_users:
        typing_users[room_code].pop(sid, None)

    # Notify room
    socketio.emit(
        "system_message",
        {
            "text": f"{username} disconnected.",
            "type": "leave",
            "timestamp": timestamp(),
        },
        room=room_code,
    )

    broadcast_typing(room_code, sid)
    broadcast_user_list(room_code)
    cleanup_room(room_code)

    print(f"[-] {username} disconnected from room '{room_code}'")


# ─────────────────────────────────────────
#  Join (alias registration)
# ─────────────────────────────────────────
@socketio.on("join")
def on_join(data):
    """
    Step 1: client registers an alias.
    Does NOT place the user into a chat room yet — that happens
    when the client subsequently emits join_global or create_room
    or join_room.
    """
    sid = request.sid
    username = data.get("username", "").strip()

    if not username:
        emit("join_error", {"message": "Alias cannot be empty."})
        return

    if len(username) > 20:
        emit("join_error", {"message": "Alias too long (max 20 chars)."})
        return

    # Check uniqueness across all connected users
    taken = {u["username"].lower() for u in connected_users.values()}
    if username.lower() in taken:
        emit("join_error", {"message": "Alias already taken."})
        return

    connected_users[sid] = {
        "username": username,
        "room": None,
        "joined_at": timestamp(),
    }

    emit("join_success", {"username": username})
    print(f"[*] {username} registered (sid={sid})")


# ─────────────────────────────────────────
#  Join global chat
# ─────────────────────────────────────────
@socketio.on("join_global")
def on_join_global():
    sid = request.sid

    if sid not in connected_users:
        emit("room_error", {"message": "Not registered. Reload and try again."})
        return

    username = connected_users[sid]["username"]
    place_user_in_room(sid, GLOBAL_ROOM)

    emit("room_joined", {"room_code": GLOBAL_ROOM, "type": "global"})

    socketio.emit(
        "system_message",
        {
            "text": f"{username} connected to global relay.",
            "type": "join",
            "timestamp": timestamp(),
        },
        room=GLOBAL_ROOM,
    )

    broadcast_user_list(GLOBAL_ROOM)
    print(f"[G] {username} joined global chat")


# ─────────────────────────────────────────
#  Create private room
# ─────────────────────────────────────────
@socketio.on("create_room")
def on_create_room():
    sid = request.sid

    if sid not in connected_users:
        emit("room_error", {"message": "Not registered. Reload and try again."})
        return

    username = connected_users[sid]["username"]
    room_code = generate_room_code()

    place_user_in_room(sid, room_code)

    emit("room_created", {"room_code": room_code, "type": "private"})

    socketio.emit(
        "system_message",
        {
            "text": f"{username} created this room.",
            "type": "join",
            "timestamp": timestamp(),
        },
        room=room_code,
    )

    broadcast_user_list(room_code)
    print(f"[R] {username} created private room '{room_code}'")


# ─────────────────────────────────────────
#  Join private room by code
# ─────────────────────────────────────────
@socketio.on("join_room")
def on_join_room(data):
    sid = request.sid

    if sid not in connected_users:
        emit("room_error", {"message": "Not registered. Reload and try again."})
        return

    room_code = data.get("room", "").strip().upper()

    if not room_code:
        emit("room_error", {"message": "Room code cannot be empty."})
        return

    if room_code == GLOBAL_ROOM:
        emit("room_error", {"message": "Invalid room code."})
        return

    if room_code not in rooms:
        emit("room_error", {"message": "Room not found. Check the code and try again."})
        return

    username = connected_users[sid]["username"]
    place_user_in_room(sid, room_code)

    emit("room_joined", {"room_code": room_code, "type": "private"})

    socketio.emit(
        "system_message",
        {
            "text": f"{username} joined the room.",
            "type": "join",
            "timestamp": timestamp(),
        },
        room=room_code,
    )

    broadcast_user_list(room_code)
    print(f"[R] {username} joined private room '{room_code}'")


# ─────────────────────────────────────────
#  Message
# ─────────────────────────────────────────
@socketio.on("message")
def on_message(data):
    sid = request.sid

    if sid not in connected_users:
        return

    user = connected_users[sid]
    room_code = user.get("room")

    if not room_code:
        return

    text = data.get("text", "").strip()
    if not text:
        return

    if len(text) > 500:
        text = text[:500]

    socketio.emit(
        "message",
        {
            "username": user["username"],
            "text": text,
            "timestamp": timestamp(),
            "sid": sid,
        },
        room=room_code,
    )


# ─────────────────────────────────────────
#  Typing indicator
# ─────────────────────────────────────────
@socketio.on("typing")
def on_typing(data):
    sid = request.sid

    if sid not in connected_users:
        return

    user = connected_users[sid]
    room_code = user.get("room")

    if not room_code or room_code not in typing_users:
        return

    username = user["username"]
    is_typing = bool(data.get("typing", False))

    if is_typing:
        typing_users[room_code][sid] = username
    else:
        typing_users[room_code].pop(sid, None)

    broadcast_typing(room_code, sid)


# ─────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[*] DARKNET server starting on port {port}")
    socketio.run(app, host="0.0.0.0", port=port)
  
