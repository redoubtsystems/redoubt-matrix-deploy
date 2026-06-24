from flask import Flask, request, render_template, redirect, url_for, flash, session
from functools import wraps
import os
import secrets
import smtplib
import threading
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
from urllib.parse import quote

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]


## Config
SERVER_NAME = os.getenv("SERVER_NAME")
if not SERVER_NAME:
    raise RuntimeError("SERVER_NAME is not set")

SYNAPSE_URL = os.getenv("SYNAPSE_URL")
if not SYNAPSE_URL:
    raise RuntimeError("SYNAPSE_URL is not set")

ADMIN_V2  = f"{SYNAPSE_URL}/_synapse/admin/v2/"
ADMIN_V1  = f"{SYNAPSE_URL}/_synapse/admin/v1/"
CLIENT_V3 = f"{SYNAPSE_URL}/_matrix/client/v3/"

SMTP_SERVER   = os.getenv("SMTP_SERVER", "")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM     = os.getenv("SMTP_FROM", "")
CHAT_URL      = os.getenv("CHAT_URL", "")
ADMIN_URL     = os.getenv("ADMIN_URL", "")
MAX_USERS     = int(os.getenv("MAX_USERS", "0"))

BILLING_SERVICE_URL    = os.getenv("BILLING_SERVICE_URL", "")
BILLING_SERVICE_SECRET = os.getenv("BILLING_SERVICE_SECRET", "")
BILLING_CUSTOMER_ID    = os.getenv("BILLING_CUSTOMER_ID", "")


## Error class
class SynapseAPIError(Exception):
    def __init__(self, message, status_code=500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


## Helpers
def _auth_headers():
    return {
        "Authorization": f"Bearer {session['access_token']}",
        "Content-Type": "application/json",
    }


def synapse_request(method, url, **kwargs):
    try:
        r = requests.request(
            method,
            url,
            headers=_auth_headers(),
            timeout=5,
            **kwargs,
        )
    except requests.RequestException:
        raise SynapseAPIError("Cannot reach Synapse", 502)

    if not r.ok:
        try:
            error = r.json().get("error", r.text)
        except ValueError:
            error = r.text
        raise SynapseAPIError(error, r.status_code)

    return r.json() if r.content else None


def _active_user_count():
    data = synapse_request(
        "GET",
        f"{ADMIN_V2}users",
        params={"limit": 1, "deactivated": "false"},
    )
    return data.get("total", 0)


def _send_invite_email(to_email, username, password):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your account on {SERVER_NAME}"
    msg["From"]    = SMTP_FROM
    msg["To"]      = to_email

    plain = (
        f"An account has been created for you on {SERVER_NAME}.\n\n"
        f"Username: {username}\n"
        f"Password: {password}\n\n"
        f"Desktop: open your browser and go to {CHAT_URL}\n\n"
        f"Mobile: install Element X, tap Sign in manually, tap Change account provider,\n"
        f"select Other, and enter the server address: {SERVER_NAME}\n\n"
        f"You can change your password in the app settings after your first login.\n\n"
        f"If you were not expecting this message, you can safely ignore it.\n"
    )

    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "redoubt_login_email.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("REDOUBT_SERVER_NAME", SERVER_NAME)
    html = html.replace("REDOUBT_USERNAME", username)
    html = html.replace("REDOUBT_PASSWORD", password)
    html = html.replace("REDOUBT_CHAT_URL", CHAT_URL)

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, to_email, msg.as_string())


def _format_bytes(n):
    """Format a byte count as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


_PLAN_DISPLAY = {
    "starter": "Starter",
    "community": "Community",
    "community_complete": "Community Complete",
    "organization": "Organization",
    "enterprise": "Enterprise",
}

# Base plan limits — mirrors inventory.yml plans block in MatrixDeploy.
# community_complete shares community's base limits; addons (video, custom domain)
# are reflected separately via the subscription data.
_PLAN_FEATURES = {
    "starter":            {"max_users": 15,  "storage_gb": 2,  "max_upload": "10 MB"},
    "community":          {"max_users": 40,  "storage_gb": 10, "max_upload": "25 MB"},
    "community_complete": {"max_users": 40,  "storage_gb": 10, "max_upload": "25 MB"},
    "organization":       {"max_users": 150, "storage_gb": 50, "max_upload": "50 MB"},
}


def _billing_request(method, path, **kwargs):
    """Make an authenticated request to the Billing Service.

    Returns the parsed JSON response, or None if billing is not configured
    or the request fails. Never raises.
    """
    if not BILLING_SERVICE_URL or not BILLING_SERVICE_SECRET or not BILLING_CUSTOMER_ID:
        return None
    try:
        r = requests.request(
            method,
            f"{BILLING_SERVICE_URL}{path}",
            headers={"Authorization": f"Bearer {BILLING_SERVICE_SECRET}"},
            timeout=10,
            **kwargs,
        )
        return r.json() if r.ok and r.content else None
    except requests.RequestException:
        return None


def _room_id(localpart):
    """Reconstruct a full Matrix room ID from its opaque localpart, URL-encoded for API paths."""
    return quote(f"!{localpart}:{SERVER_NAME}", safe="")


def _bg_request(method, url, access_token, **kwargs):
    """Minimal request wrapper for background threads (no Flask context)."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.request(method, url, headers=headers, timeout=10, **kwargs)
        return r.json() if r.ok and r.content else None
    except Exception:
        return None


def _auto_join_to_public_rooms(user_id, access_token):
    """Background: force-join a new user into every existing public room."""
    data = _bg_request("GET", f"{ADMIN_V1}rooms", access_token, params={"limit": 1000})
    if not data:
        return
    for room in data.get("rooms", []):
        if room.get("public") and room.get("room_id"):
            _bg_request(
                "POST",
                f"{ADMIN_V1}join/{quote(room['room_id'], safe='')}",
                access_token,
                json={"user_id": user_id},
            )


def _auto_join_all_to_room(room_id, access_token):
    """Background: force-join all active users into a newly created public room."""
    encoded_room = quote(room_id, safe="")
    data = _bg_request(
        "GET",
        f"{ADMIN_V2}users",
        access_token,
        params={"limit": 1000, "deactivated": "false"},
    )
    if not data:
        return
    for user in data.get("users", []):
        uid = user.get("name")
        if uid:
            _bg_request(
                "POST",
                f"{ADMIN_V1}join/{encoded_room}",
                access_token,
                json={"user_id": uid},
            )


## Decorators
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "access_token" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


## Template context
@app.context_processor
def inject_globals():
    return {
        "server_name": SERVER_NAME,
        "current_user": session.get("user_id"),
    }


## Error handlers
@app.errorhandler(SynapseAPIError)
def handle_synapse_error(error):
    if error.status_code == 401:
        session.clear()
        flash("Your session has expired. Please log in again.", "warning")
        return redirect(url_for("login"))
    flash(error.message, "error")
    return (
        render_template(
            "error.html",
            title="Administration Error",
            message=error.message,
            status=error.status_code,
        ),
        error.status_code,
    )


@app.errorhandler(404)
def not_found(e):
    return render_template(
        "error.html",
        title="Not Found",
        message="The requested page does not exist.",
        status=404,
    ), 404


@app.errorhandler(500)
def internal_error(e):
    return render_template(
        "error.html",
        title="Internal Error",
        message="Something went wrong on the server.",
        status=500,
    ), 500


## Auth routes
@app.route("/")
def index():
    return redirect(url_for("get_users"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if "access_token" in session:
        return redirect(url_for("get_users"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password are required.", "error")
            return render_template("login.html")

        user_id = (
            username if username.startswith("@")
            else f"@{username}:{SERVER_NAME}"
        )

        try:
            r = requests.post(
                f"{SYNAPSE_URL}/_matrix/client/v3/login",
                json={
                    "type": "m.login.password",
                    "identifier": {"type": "m.id.user", "user": user_id},
                    "password": password,
                },
                timeout=5,
            )
        except requests.RequestException:
            flash("Cannot reach Synapse. Try again later.", "error")
            return render_template("login.html")

        if not r.ok:
            try:
                msg = r.json().get("error", "Login failed.")
            except ValueError:
                msg = "Login failed."
            flash(msg, "error")
            return render_template("login.html")

        data = r.json()
        token = data["access_token"]

        # Verify admin status before granting portal access
        check = requests.get(
            f"{ADMIN_V2}users",
            headers={"Authorization": f"Bearer {token}"},
            params={"limit": 1},
            timeout=5,
        )
        if check.status_code == 403:
            flash("This account does not have admin privileges.", "error")
            return render_template("login.html")

        session["access_token"] = token
        session["user_id"] = data.get("user_id", user_id)
        return redirect(url_for("get_users"))

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    if "access_token" in session:
        try:
            requests.post(
                f"{SYNAPSE_URL}/_matrix/client/v3/logout",
                headers={"Authorization": f"Bearer {session['access_token']}"},
                timeout=5,
            )
        except Exception:
            pass  # Best effort — clear session regardless
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("login"))


## User routes
@app.route("/users", methods=["GET"])
@login_required
def get_users():
    data = synapse_request(
        "GET",
        f"{ADMIN_V2}users",
        params={
            "from": request.args.get("from", 0),
            "limit": request.args.get("limit", 100),
        },
    )

    users = data.get("users", [])
    for user in users:
        name = user.get("name")
        if isinstance(name, str) and name.startswith("@"):
            user["localpart"] = name.split(":", 1)[0][1:]
        else:
            user["localpart"] = name or "unknown"

    return render_template("users.html", users=users)


@app.route("/users", methods=["POST"])
@login_required
def create_user():
    data = request.form
    admin = data.get("admin") == "true"
    username = data.get("username", "").strip()
    user_id = f"@{username}:{SERVER_NAME}"

    if MAX_USERS and _active_user_count() >= MAX_USERS:
        flash(
            f"User limit reached ({MAX_USERS} active users). "
            "Deactivate a user or upgrade your plan before adding more.",
            "error",
        )
        return redirect(url_for("new_user_form"))

    if data.get("invite_mode") == "email":
        email = data.get("email", "").strip()
        initial_password = secrets.token_urlsafe(32)

        synapse_request(
            "PUT",
            f"{ADMIN_V2}users/{user_id}",
            json={"password": initial_password, "admin": admin, "deactivated": False},
        )

        _token = session["access_token"]
        threading.Thread(
            target=_auto_join_to_public_rooms, args=(user_id, _token), daemon=True
        ).start()

        try:
            _send_invite_email(email, username, initial_password)
            flash(f"User created. Credentials emailed to {email}.", "success")
        except Exception as e:
            flash(f"User created but email failed to send: {e}", "warning")
    else:
        synapse_request(
            "PUT",
            f"{ADMIN_V2}users/{user_id}",
            json={"password": data["password"], "admin": admin, "deactivated": False},
        )
        _token = session["access_token"]
        threading.Thread(
            target=_auto_join_to_public_rooms, args=(user_id, _token), daemon=True
        ).start()
        flash(f"User {user_id} created successfully.", "success")

    return redirect(url_for("get_users"))


@app.route("/users/new", methods=["GET"])
@login_required
def new_user_form():
    active = _active_user_count()
    return render_template(
        "userCreate.html",
        active_users=active,
        max_users=MAX_USERS,
    )


@app.route("/users/<username>", methods=["GET"])
@login_required
def get_user(username):
    user_id = f"@{username}:{SERVER_NAME}"
    data = synapse_request("GET", f"{ADMIN_V2}users/{user_id}")
    return render_template("userDetail.html", user=data)


@app.route("/users/<username>/deactivate", methods=["POST"])
@login_required
def deactivate_user(username):
    user_id = f"@{username}:{SERVER_NAME}"
    synapse_request("POST", f"{ADMIN_V1}deactivate/{user_id}")
    flash(f"User {user_id} has been deactivated.", "warning")
    return redirect(url_for("get_users"))


@app.route("/users/<username>/reset-password", methods=["POST"])
@login_required
def reset_password(username):
    user_id = f"@{username}:{SERVER_NAME}"
    new_password = request.form.get("new_password", "")

    if len(new_password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("get_user", username=username))

    synapse_request(
        "PUT",
        f"{ADMIN_V2}users/{user_id}",
        json={"password": new_password},
    )
    flash(f"Password updated for {user_id}.", "success")
    return redirect(url_for("get_user", username=username))


@app.route("/users/<username>/resend-invite", methods=["POST"])
@login_required
def resend_invite(username):
    user_id = f"@{username}:{SERVER_NAME}"
    email = request.form.get("email", "").strip()

    if not email:
        flash("Email address is required.", "error")
        return redirect(url_for("get_user", username=username))

    # Reset to a new random password — invalidates all existing sessions
    initial_password = secrets.token_urlsafe(32)
    synapse_request(
        "PUT",
        f"{ADMIN_V2}users/{user_id}",
        json={"password": initial_password},
    )

    try:
        _send_invite_email(email, username, initial_password)
        flash(f"Password reset. New credentials emailed to {email}.", "success")
    except Exception as e:
        flash(f"Password reset but email failed to send: {e}", "warning")

    return redirect(url_for("get_user", username=username))


## Room routes
@app.route("/rooms", methods=["GET"])
@login_required
def get_rooms():
    data = synapse_request(
        "GET",
        f"{ADMIN_V1}rooms",
        params={
            "from": request.args.get("from", 0),
            "limit": request.args.get("limit", 100),
            "order_by": "joined_members",
            "dir": "b",
        },
    )

    all_rooms = data.get("rooms", [])
    for room in all_rooms:
        rid = room.get("room_id", "")
        # Strip "!" prefix and ":server_name" suffix for a URL-safe localpart
        room["localpart"] = rid.lstrip("!").split(":")[0] if rid else ""

    total = data.get("total_rooms", 0)
    show_all = request.args.get("show_all", "0") == "1"

    if show_all:
        rooms = all_rooms
    else:
        rooms = [r for r in all_rooms if r.get("name") or r.get("canonical_alias")]

    return render_template("rooms.html", rooms=rooms, total=total, show_all=show_all)


@app.route("/rooms/new", methods=["GET"])
@login_required
def new_room_form():
    return render_template("roomCreate.html")


@app.route("/rooms", methods=["POST"])
@login_required
def create_room():
    name       = request.form.get("name", "").strip()
    alias      = request.form.get("alias", "").strip()
    topic      = request.form.get("topic", "").strip()
    visibility = request.form.get("visibility", "private")

    body: dict = {
        "name": name,
        "preset": "public_chat" if visibility == "public" else "private_chat",
    }
    if alias:
        body["room_alias_name"] = alias
    if topic:
        body["topic"] = topic

    result = synapse_request("POST", f"{CLIENT_V3}createRoom", json=body)

    if visibility == "public" and result:
        _token = session["access_token"]
        threading.Thread(
            target=_auto_join_all_to_room, args=(result["room_id"], _token), daemon=True
        ).start()

    flash(f"Room '{name}' created.", "success")
    return redirect(url_for("get_rooms"))


@app.route("/rooms/<room_localpart>", methods=["GET"])
@login_required
def get_room(room_localpart):
    room_id = _room_id(room_localpart)
    room    = synapse_request("GET", f"{ADMIN_V1}rooms/{room_id}")
    members = synapse_request("GET", f"{ADMIN_V1}rooms/{room_id}/members")
    return render_template(
        "roomDetail.html",
        room=room,
        members=members.get("members", []),
        room_localpart=room_localpart,
    )


@app.route("/rooms/<room_localpart>/invite", methods=["POST"])
@login_required
def room_invite(room_localpart):
    room_id  = _room_id(room_localpart)
    username = request.form.get("username", "").strip()
    user_id  = username if username.startswith("@") else f"@{username}:{SERVER_NAME}"

    synapse_request("POST", f"{ADMIN_V1}join/{room_id}", json={"user_id": user_id})
    flash(f"Added {user_id} to the room.", "success")
    return redirect(url_for("get_room", room_localpart=room_localpart))


@app.route("/rooms/<room_localpart>/remove-user", methods=["POST"])
@login_required
def room_remove_user(room_localpart):
    room_id    = _room_id(room_localpart)
    user_id    = request.form.get("user_id", "").strip()
    admin_user = session.get("user_id")

    # Ensure the admin is in the room before kicking (no-op if already a member)
    synapse_request("POST", f"{ADMIN_V1}join/{room_id}", json={"user_id": admin_user})

    synapse_request(
        "POST",
        f"{CLIENT_V3}rooms/{room_id}/kick",
        json={"user_id": user_id, "reason": "Removed by administrator"},
    )
    flash(f"Removed {user_id} from the room.", "success")
    return redirect(url_for("get_room", room_localpart=room_localpart))


@app.route("/rooms/<room_localpart>/delete", methods=["POST"])
@login_required
def delete_room(room_localpart):
    room_id = _room_id(room_localpart)
    synapse_request(
        "DELETE",
        f"{ADMIN_V2}rooms/{room_id}",
        json={"block": True, "purge": True},
    )
    flash("Room queued for deletion. It may take a moment to disappear.", "warning")
    return redirect(url_for("get_rooms"))


## Server status route
@app.route("/server", methods=["GET"])
@login_required
def get_server_status():
    version_data = synapse_request("GET", f"{ADMIN_V1}server_version")
    active_users = _active_user_count()
    room_data = synapse_request("GET", f"{ADMIN_V1}rooms", params={"limit": 1})
    media_data = synapse_request(
        "GET",
        f"{ADMIN_V1}statistics/users/media",
        params={"limit": 100, "order_by": "media_length", "dir": "b"},
    )
    try:
        db_rooms_data = synapse_request("GET", f"{ADMIN_V1}statistics/database/rooms")
        db_rooms = db_rooms_data.get("rooms", [])
    except SynapseAPIError:
        db_rooms = None

    media_users = media_data.get("users", [])
    total_media_bytes = sum(u.get("media_length", 0) for u in media_users)

    return render_template(
        "server.html",
        server_version=version_data.get("server_version", "Unknown"),
        active_users=active_users,
        max_users=MAX_USERS,
        total_rooms=room_data.get("total_rooms", 0),
        media_users=media_users,
        total_media=_format_bytes(total_media_bytes),
        db_rooms=db_rooms,
        format_bytes=_format_bytes,
    )


## Subscription routes
@app.route("/subscription", methods=["GET"])
@login_required
def subscription():
    data = _billing_request(
        "GET",
        f"/internal/tenants/{BILLING_CUSTOMER_ID}/subscription",
    )

    period_end_display = None
    if data and data.get("current_period_end"):
        period_end_display = datetime.fromtimestamp(
            data["current_period_end"], tz=timezone.utc
        ).strftime("%B %-d, %Y")

    plan_type = (data or {}).get("plan_type", "")
    plan_display = _PLAN_DISPLAY.get(plan_type, plan_type)
    plan_features = _PLAN_FEATURES.get(plan_type)

    extra_storage = (data or {}).get("extra_storage_gb", 0) or 0
    total_storage_gb = None
    if plan_features:
        total_storage_gb = plan_features["storage_gb"] + extra_storage

    return render_template(
        "subscription.html",
        sub=data,
        plan_display=plan_display,
        plan_features=plan_features,
        period_end_display=period_end_display,
        billing_configured=bool(BILLING_SERVICE_URL and BILLING_CUSTOMER_ID),
        max_users=MAX_USERS,
        total_storage_gb=total_storage_gb,
    )


@app.route("/subscription/portal", methods=["POST"])
@login_required
def subscription_portal():
    return_url = f"{ADMIN_URL}/subscription"
    result = _billing_request(
        "POST",
        f"/internal/tenants/{BILLING_CUSTOMER_ID}/portal-session",
        json={"return_url": return_url},
    )
    if not result or "url" not in result:
        flash("Unable to open the billing portal. Please try again or contact support@redoubt.systems.", "error")
        return redirect(url_for("subscription"))
    return redirect(result["url"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
