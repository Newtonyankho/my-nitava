# app.py
"""
Extended social platform backend with role levels:
  - roles: user, intern, moderator, admin, developer
  - developer: can assign all roles (including admin)
  - admin: can assign moderator & intern (and demote them)
  - moderator: moderation-only (delete flagged content)
  - intern: review-only
Features added:
  - role-change audit log (RoleChange)
  - Notification + persistent storage + socket emit
  - Reports for posts (Report)
  - Post edit endpoint
  - User activate/deactivate (ban/unban)
  - Extra profile fields on User (auto-added to SQLite if missing)
  - Safe permission checks for promotion/demotion
  - If DB empty: creates an initial developer account (printed to console)
Note: uses Flask-SocketIO (eventlet) for realtime notifications
"""
import os
import json
from datetime import datetime
from functools import wraps
from flask import (
    Flask, request, jsonify, session, send_from_directory, render_template, abort
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_migrate import Migrate
from PIL import Image
from io import BytesIO


BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'app.db')}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "uploads")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
# Flask-Migrate (DB migrations)
migrate = Migrate(app, db)


# ---------------- Constants / Roles ----------------
ROLE_USER = "user"
ROLE_INTERN = "intern"
ROLE_MODERATOR = "moderator"
ROLE_ADMIN = "admin"
ROLE_DEVELOPER = "developer"

VALID_ROLES = {ROLE_USER, ROLE_INTERN, ROLE_MODERATOR, ROLE_ADMIN, ROLE_DEVELOPER}

# ---------------- Models ----------------
followers = db.Table(
    "followers",
    db.Column("follower_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
    db.Column("followed_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fullname = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

    # Role + profile fields
    role = db.Column(db.String(30), default=ROLE_USER)  # e.g. user, intern, moderator, admin, developer
    profile_pic = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Extended profile (some may be NULL initially; we attempt to auto-add columns for SQLite)
    bio = db.Column(db.Text)
    education = db.Column(db.String(200))
    hometown = db.Column(db.String(200))
    district = db.Column(db.String(200))
    dob = db.Column(db.String(50))
    religion = db.Column(db.String(100))
    marital_status = db.Column(db.String(50))
    technical_skills = db.Column(db.Text)
    soft_skills = db.Column(db.Text)
    languages = db.Column(db.String(200))
    hobbies = db.Column(db.Text)
    job = db.Column(db.String(200))
    phone = db.Column(db.String(50))
    interested_in = db.Column(db.String(200))
    active = db.Column(db.Boolean, default=True)  # soft-ban flag

    # follower relationship
    followed = db.relationship(
        "User",
        secondary=followers,
        primaryjoin=(followers.c.follower_id == id),
        secondaryjoin=(followers.c.followed_id == id),
        backref=db.backref("followers", lazy="dynamic"),
        lazy="dynamic",
    )

    def to_dict(self, public=True):
        base = {
            "id": self.id,
            "fullname": self.fullname,
            "email": self.email if not public else None,  # hide email in public calls unless explicit
            "role": self.role,
            "profile_pic": f"/uploads/{self.profile_pic}" if self.profile_pic else None,
            "created_at": self.created_at.isoformat(),
            "active": bool(self.active)
        }
        # include extended profile
        base.update({
            "bio": self.bio,
            "education": self.education,
            "hometown": self.hometown,
            "district": self.district,
            "dob": self.dob,
            "religion": self.religion,
            "marital_status": self.marital_status,
            "technical_skills": self.technical_skills,
            "soft_skills": self.soft_skills,
            "languages": self.languages,
            "hobbies": self.hobbies,
            "job": self.job,
            "phone": self.phone,
            "interested_in": self.interested_in
        })
        return base

class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship("User", backref=db.backref("posts", lazy="dynamic"))

    def to_dict(self):
        return {
            "id": self.id,
            "user": {
                "id": self.user.id,
                "fullname": self.user.fullname,
                "profile_pic": f"/uploads/{self.user.profile_pic}" if self.user.profile_pic else None,
                "role": self.user.role
            },
            "content": self.content,
            "created_at": self.created_at.isoformat(),
            "likes": [u.user_id for u in self.likes],
            "comments": [c.to_dict() for c in self.comments]
        }

class Like(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship("User", lazy="joined")
    post = db.relationship("Post", backref=db.backref("likes", lazy="dynamic"))

class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=False)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship("User", lazy="joined")
    post = db.relationship("Post", backref=db.backref("comments", lazy="dynamic"))

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)  # target user
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)  # who triggered (optional)
    type = db.Column(db.String(80))
    message = db.Column(db.Text)
    data = db.Column(db.Text)  # json string
    read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        d = {
            "id": self.id,
            "user_id": self.user_id,
            "actor_id": self.actor_id,
            "type": self.type,
            "message": self.message,
            "data": json.loads(self.data) if self.data else None,
            "read": bool(self.read),
            "created_at": self.created_at.isoformat()
        }
        return d

class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=True)
    reason = db.Column(db.Text)
    resolved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "reporter_id": self.reporter_id,
            "post_id": self.post_id,
            "reason": self.reason,
            "resolved": bool(self.resolved),
            "created_at": self.created_at.isoformat()
        }

class RoleChange(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)  # who changed
    target_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)  # who was changed
    old_role = db.Column(db.String(50))
    new_role = db.Column(db.String(50))
    reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "actor_id": self.actor_id,
            "target_id": self.target_id,
            "old_role": self.old_role,
            "new_role": self.new_role,
            "reason": self.reason,
            "created_at": self.created_at.isoformat()
        }

# ---------------- Helpers ----------------
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "authentication required"}), 401
        # verify account active
        user = User.query.get(session["user_id"])
        if not user or not user.active:
            return jsonify({"error": "account_inactive"}), 403
        return fn(*args, **kwargs)
    return wrapper

def role_required(roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            uid = session.get("user_id")
            if not uid:
                return jsonify({"error": "authentication required"}), 401
            user = User.query.get(uid)
            if not user or user.role not in roles:
                return jsonify({"error": "forbidden"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator

def can_assign_role(actor: User, target: User, new_role: str) -> (bool, str):
    """
    Enforce role-change policy:
      - developer can assign any role
      - admin can assign moderator/intern/user but cannot assign admin/developer or change developers/admins
      - no one can change a developer unless they are developer
      - prevent accidental self-role-change unless developer
    Returns (allowed, reason)
    """
    new_role = new_role.lower().strip()
    if new_role not in VALID_ROLES:
        return False, "invalid_target_role"

    actor_role = actor.role
    target_role = target.role

    # Prevent non-developers changing developer accounts
    if target_role == ROLE_DEVELOPER and actor_role != ROLE_DEVELOPER:
        return False, "cannot_modify_developer"

    # Prevent admins changing developers
    if actor_role == ROLE_ADMIN and target_role in (ROLE_ADMIN, ROLE_DEVELOPER):
        return False, "admin_cannot_modify_admin_or_developer"

    # Prevent self role-change for safety, except developer
    if actor.id == target.id and actor_role != ROLE_DEVELOPER:
        return False, "cannot_change_own_role"

    # Developer can do everything
    if actor_role == ROLE_DEVELOPER:
        return True, "ok"

    # Admin can assign moderator, intern, user (but not admin or developer)
    if actor_role == ROLE_ADMIN:
        if new_role in (ROLE_MODERATOR, ROLE_INTERN, ROLE_USER):
            # but admin cannot change other admins/developers (handled above)
            return True, "ok"
        return False, "admin_can_only_assign_moderator_intern_user"

    # Others cannot assign roles
    return False, "insufficient_permissions"

def create_notification(target_user_id, actor_id=None, typ=None, message=None, data_obj=None, emit_real_time=True):
    n = Notification(
        user_id=target_user_id,
        actor_id=actor_id,
        type=typ,
        message=message,
        data=json.dumps(data_obj) if data_obj is not None else None,
        read=False
    )
    db.session.add(n)
    db.session.commit()
    payload = n.to_dict()
    if emit_real_time:
        socketio.emit("notification", payload, room=f"user_{target_user_id}")
    return n

# ---------------- DB Schema helper (auto-add missing user columns for SQLite) ----------------
def ensure_user_columns():
    """
    Adds missing columns to `user` table in SQLite using ALTER TABLE ADD COLUMN.
    This reduces migration friction for development. If you prefer migrations,
    use Flask-Migrate instead.
    """
    # Only run for SQLite (we use PRAGMA)
    if not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        return

    columns_to_add = {
        "bio": "TEXT",
        "education": "TEXT",
        "hometown": "TEXT",
        "district": "TEXT",
        "dob": "TEXT",
        "religion": "TEXT",
        "marital_status": "TEXT",
        "technical_skills": "TEXT",
        "soft_skills": "TEXT",
        "languages": "TEXT",
        "hobbies": "TEXT",
        "job": "TEXT",
        "phone": "TEXT",
        "interested_in": "TEXT",
        "active": "INTEGER DEFAULT 1"
    }
    try:
        with db.engine.begin() as conn:
            res = conn.execute(text("PRAGMA table_info('user')"))
            existing = [row['name'] for row in res.mappings().all()]
            for col, sqltype in columns_to_add.items():
                if col not in existing:
                    sql = f"ALTER TABLE user ADD COLUMN {col} {sqltype}"
                    conn.execute(text(sql))
                    print(f"[schema] added column {col} to user table ({sqltype})")
    except Exception as e:
        print("[schema] ensure_user_columns failed:", e)
# ---- Image helper: validate + resize ----
ALLOWED_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp"}
MAX_IMAGE_SIZE_BYTES = 3 * 1024 * 1024  # 3 MB
PROFILE_PIC_MAX_DIM = 512  # max width/height in px

def allowed_image_filename(filename):
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_IMAGE_EXTS

def process_and_save_image(file_storage, prefix="img"):
    """
    Validates file type & size, then resizes to PROFILE_PIC_MAX_DIM and saves.
    Returns saved filename or raises ValueError on invalid file.
    """
    # quick size check (if Content-Length available)
    try:
        file_storage.stream.seek(0, os.SEEK_END)
        size = file_storage.stream.tell()
        file_storage.stream.seek(0)
    except Exception:
        size = None

    if size is not None and size > MAX_IMAGE_SIZE_BYTES:
        raise ValueError("file_too_large")

    filename = secure_filename(file_storage.filename or "upload")
    if not allowed_image_filename(filename):
        raise ValueError("invalid_image_type")
    ext = filename.rsplit(".", 1)[1].lower()
    # load via Pillow
    img = Image.open(file_storage.stream).convert("RGB")
    # resize (maintain aspect)
    img.thumbnail((PROFILE_PIC_MAX_DIM, PROFILE_PIC_MAX_DIM))
    out_name = f"{prefix}_{int(datetime.utcnow().timestamp())}.{ext}"
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], out_name)
    # save optimized JPEG/PNG
    if ext in ("jpg", "jpeg"):
        img.save(save_path, format="JPEG", quality=85, optimize=True)
    elif ext == "png":
        img.save(save_path, format="PNG", optimize=True)
    elif ext == "webp":
        img.save(save_path, format="WEBP", quality=85, method=6)
    else:
        # fallback
        img.save(save_path)
    return out_name


# ---------------- Routes ----------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# --- Auth & Registration ---
@app.route("/api/register", methods=["POST"])
def register():
    form = request.form
    fullname = form.get("fullname", "").strip()
    email = form.get("email", "").strip().lower()
    password = form.get("password", "").strip()
    if not fullname or not email or not password:
        return jsonify({"error": "fullname, email and password are required"}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "email_already_registered"}), 400

    profile = request.files.get("profilepic")
    filename = None
    if profile and profile.filename:
        try:
            filename = process_and_save_image(profile, prefix="profile")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": "image_processing_failed", "detail": str(e)}), 500


    # always register as plain user; role assignment is done by staff later
    user = User(
        fullname=fullname,
        email=email,
        password_hash=generate_password_hash(password),
        profile_pic=filename,
        role=ROLE_USER
    )
    db.session.add(user)
    db.session.commit()
    session["user_id"] = user.id
    return jsonify({"message": "registered", "user": user.to_dict(public=False)})

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json or request.form
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")
    if not email or not password:
        return jsonify({"error": "email and password required"}), 400
    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "invalid_credentials"}), 401
    if not user.active:
        return jsonify({"error": "account_inactive"}), 403
    session["user_id"] = user.id
    return jsonify({"message": "ok", "user": user.to_dict(public=False)})

@app.route("/api/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    return jsonify({"message": "logged out"})

@app.route("/api/me")
def me():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"user": None})
    user = User.query.get(uid)
    if not user:
        return jsonify({"user": None})
    # return private profile to logged-in user
    return jsonify({"user": user.to_dict(public=False)})

# --- Profile update (self or admin) ---
@app.route("/api/users/<int:user_id>/update", methods=["POST"])
@login_required
def update_profile(user_id):
    caller = User.query.get(session["user_id"])
    target = User.query.get_or_404(user_id)
    # allow self or admins/moderators to edit
    if caller.id != target.id and caller.role not in (ROLE_ADMIN, ROLE_MODERATOR, ROLE_DEVELOPER):
        return jsonify({"error": "forbidden"}), 403

    form = request.form
    # simple updatable fields
    updatable = ["fullname", "bio", "education", "hometown", "district", "dob",
                 "religion", "marital_status", "technical_skills", "soft_skills",
                 "languages", "hobbies", "job", "phone", "interested_in"]
    for k in updatable:
        if k in form:
            setattr(target, k, form.get(k))

    # profile pic
    profile = request.files.get("profilepic")
    filename = None
    if profile and profile.filename:
        try:
            filename = process_and_save_image(profile, prefix="profile")
            target.profile_pic = filename
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": "image_processing_failed", "detail": str(e)}), 500

    db.session.commit()
    return jsonify({"user": target.to_dict(public=False)})

# --- User listing (admin/dev) ---
@app.route("/api/admin/users", methods=["GET"])
@role_required([ROLE_ADMIN, ROLE_DEVELOPER])
def list_users_admin():
    page = max(int(request.args.get("page") or 1), 1)
    per_page = min(int(request.args.get("per_page") or 25), 200)
    q = User.query.order_by(User.created_at.desc())
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    items = [u.to_dict(public=True) for u in pagination.items]
    return jsonify({
        "users": items,
        "page": page,
        "per_page": per_page,
        "total": pagination.total,
        "pages": pagination.pages
    })

# --- Role change endpoint ---
@app.route("/api/users/<int:target_id>/role", methods=["POST"])
@login_required
def change_role(target_id):
    actor = User.query.get(session["user_id"])
    target = User.query.get_or_404(target_id)
    data = request.get_json() or request.form
    new_role = (data.get("role") or "").strip().lower()
    reason = data.get("reason", "")
    if not new_role:
        return jsonify({"error": "role required"}), 400
    allowed, msg = can_assign_role(actor, target, new_role)
    if not allowed:
        return jsonify({"error": msg}), 403

    old_role = target.role
    target.role = new_role
    # record role change
    rc = RoleChange(actor_id=actor.id, target_id=target.id, old_role=old_role, new_role=new_role, reason=reason)
    db.session.add(rc)
    db.session.commit()

    # notify target
    create_notification(
        target_user_id=target.id,
        actor_id=actor.id,
        typ="role_changed",
        message=f"Your role changed from {old_role} to {new_role}",
        data_obj={"old_role": old_role, "new_role": new_role, "reason": reason}
    )

    # emit real-time event
    socketio.emit("role_changed", rc.to_dict(), room=f"user_{target.id}")

    return jsonify({"message": "role_changed", "role_change": rc.to_dict(), "user": target.to_dict(public=False)})

# --- View role-change audit (admin/dev) ---
@app.route("/api/role_changes", methods=["GET"])
@role_required([ROLE_ADMIN, ROLE_DEVELOPER])
def role_changes():
    page = max(int(request.args.get("page") or 1), 1)
    per_page = min(int(request.args.get("per_page") or 25), 200)
    q = RoleChange.query.order_by(RoleChange.created_at.desc())
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    items = [r.to_dict() for r in pagination.items]
    return jsonify({"role_changes": items, "page": page, "per_page": per_page, "total": pagination.total, "pages": pagination.pages})

# --- Activate / Deactivate user (ban/unban) ---
@app.route("/api/users/<int:target_id>/deactivate", methods=["POST"])
@login_required
def deactivate_user(target_id):
    actor = User.query.get(session["user_id"])
    target = User.query.get_or_404(target_id)
    if actor.role not in (ROLE_ADMIN, ROLE_DEVELOPER):
        return jsonify({"error": "forbidden"}), 403
    # admins cannot deactivate admins or developers
    if actor.role == ROLE_ADMIN and target.role in (ROLE_ADMIN, ROLE_DEVELOPER):
        return jsonify({"error": "admin_cannot_deactivate_admin_or_developer"}), 403
    data = request.get_json() or request.form
    action = (data.get("action") or "deactivate").lower()  # "deactivate" or "activate"
    if action == "deactivate":
        target.active = False
        msg = "deactivated"
    else:
        target.active = True
        msg = "activated"
    db.session.commit()
    create_notification(target.id, actor.id, "account_status", f"Your account was {msg} by {actor.fullname}", {"action": action})
    socketio.emit("account_status", {"user_id": target.id, "action": action}, room=f"user_{target.id}")
    return jsonify({"message": msg, "user": target.to_dict(public=False)})

# ---------------- Posts: create, list (paginated), edit, delete ----------------
@app.route("/api/posts", methods=["GET", "POST"])
def posts_route():
    if request.method == "GET":
        # pagination: ?page=1&per_page=10
        page = max(int(request.args.get("page") or 1), 1)
        per_page = min(int(request.args.get("per_page") or 10), 50)
        q = Post.query.order_by(Post.created_at.desc())
        pagination = q.paginate(page=page, per_page=per_page, error_out=False)
        items = [p.to_dict() for p in pagination.items]
        return jsonify({
            "posts": items,
            "page": page,
            "per_page": per_page,
            "total": pagination.total,
            "pages": pagination.pages
        })
    else:
        if "user_id" not in session:
            return jsonify({"error": "authentication required"}), 401
        data = request.get_json() or request.form
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify({"error": "content required"}), 400
        post = Post(user_id=session["user_id"], content=content)
        db.session.add(post)
        db.session.commit()
        notify_new_post(post)
        return jsonify({"post": post.to_dict()})

@app.route("/api/posts/<int:post_id>", methods=["DELETE"])
@login_required
def delete_post(post_id):
    post = Post.query.get_or_404(post_id)
    user = User.query.get(session["user_id"])
    # allow if owner or admin/moderator/developer
    if post.user_id != user.id and user.role not in (ROLE_ADMIN, ROLE_MODERATOR, ROLE_DEVELOPER):
        return jsonify({"error": "forbidden"}), 403
    db.session.delete(post)
    db.session.commit()
    socketio.emit("post_deleted", {"post_id": post_id}, broadcast=True)
    return jsonify({"message": "deleted"})

@app.route("/api/posts/<int:post_id>/edit", methods=["POST"])
@login_required
def edit_post(post_id):
    post = Post.query.get_or_404(post_id)
    user = User.query.get(session["user_id"])
    data = request.get_json() or request.form
    new_content = (data.get("content") or "").strip()
    if not new_content:
        return jsonify({"error": "content required"}), 400
    # owner or admin/moderator/developer can edit
    if post.user_id != user.id and user.role not in (ROLE_ADMIN, ROLE_MODERATOR, ROLE_DEVELOPER):
        return jsonify({"error": "forbidden"}), 403
    post.content = new_content
    db.session.commit()
    socketio.emit("post_edited", {"post_id": post.id, "content": post.content}, broadcast=True)
    return jsonify({"post": post.to_dict()})

# --- Likes ---
@app.route("/api/posts/<int:post_id>/like", methods=["POST"])
@login_required
def like_post(post_id):
    post = Post.query.get_or_404(post_id)
    uid = session["user_id"]
    existing = Like.query.filter_by(user_id=uid, post_id=post_id).first()
    if existing:
        # unlike
        db.session.delete(existing)
        db.session.commit()
        return jsonify({"liked": False})
    like = Like(user_id=uid, post_id=post_id)
    db.session.add(like)
    db.session.commit()
    # send persistent notification to post owner
    if post.user_id != uid:
        create_notification(
            target_user_id=post.user_id,
            actor_id=uid,
            typ="like",
            message=f"{User.query.get(uid).fullname} liked your post",
            data_obj={"post_id": post.id}
        )
    return jsonify({"liked": True})

# --- Comments ---
@app.route("/api/posts/<int:post_id>/comments", methods=["POST"])
@login_required
def comment_post(post_id):
    post = Post.query.get_or_404(post_id)
    data = request.json or request.form
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    comment = Comment(user_id=session["user_id"], post_id=post_id, text=text)
    db.session.add(comment)
    db.session.commit()
    # persistent notification to post owner
    if post.user_id != session["user_id"]:
        create_notification(
            target_user_id=post.user_id,
            actor_id=session["user_id"],
            typ="comment",
            message=f"{User.query.get(session['user_id']).fullname} commented on your post",
            data_obj={"post_id": post_id, "comment_id": comment.id}
        )
    return jsonify({"comment": {"id": comment.id, "text": comment.text, "user": comment.user.to_dict(public=True), "created_at": comment.created_at.isoformat()}})

# --- Report a post ---
@app.route("/api/posts/<int:post_id>/report", methods=["POST"])
@login_required
def report_post(post_id):
    post = Post.query.get_or_404(post_id)
    data = request.json or request.form
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "reason required"}), 400
    rep = Report(reporter_id=session["user_id"], post_id=post_id, reason=reason)
    db.session.add(rep)
    db.session.commit()
    # notify admins/devs via socket (they should be listening)
    socketio.emit("new_report", rep.to_dict(), broadcast=True)
    return jsonify({"report": rep.to_dict()})

# --- View reports (admin/dev) ---
@app.route("/api/reports", methods=["GET"])
@role_required([ROLE_ADMIN, ROLE_DEVELOPER])
def view_reports():
    page = max(int(request.args.get("page") or 1), 1)
    per_page = min(int(request.args.get("per_page") or 25), 200)
    q = Report.query.order_by(Report.created_at.desc())
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    items = [r.to_dict() for r in pagination.items]
    return jsonify({"reports": items, "page": page, "per_page": per_page, "total": pagination.total, "pages": pagination.pages})

# --- Follow/unfollow ---
@app.route("/api/users/<int:target_id>/follow", methods=["POST"])
@login_required
def follow_user(target_id):
    me = User.query.get(session["user_id"])
    target = User.query.get_or_404(target_id)
    if me.id == target.id:
        return jsonify({"error": "cannot_follow_self"}), 400
    if me.followed.filter_by(id=target.id).first():
        # unfollow
        me.followed.remove(target)
        db.session.commit()
        return jsonify({"followed": False})
    me.followed.append(target)
    db.session.commit()
    create_notification(target.id, me.id, "follow", f"{me.fullname} started following you", {"follower_id": me.id})
    return jsonify({"followed": True})

# ---------------- Socket.IO ----------------
@socketio.on("connect")
def on_connect():
    uid = session.get("user_id")
    if uid:
        join_room(f"user_{uid}")
        emit("connected", {"message": "connected", "user_id": uid})
    else:
        emit("connected", {"message": "connected", "user_id": None})

@socketio.on("join_chat")
def on_join_chat(data):
    room = data.get("room")
    if room:
        join_room(room)
        emit("chat_joined", {"room": room}, room=room)

@socketio.on("leave_chat")
def on_leave_chat(data):
    room = data.get("room")
    if room:
        leave_room(room)
        emit("chat_left", {"room": room}, room=room)

@socketio.on("send_message")
def on_send_message(data):
    room = data.get("room")
    text_msg = data.get("text")
    sender = session.get("user_id")
    if not sender:
        emit("error", {"error": "not authenticated"})
        return
    msg = {
        "room": room,
        "text": text_msg,
        "from_user": sender,
        "ts": datetime.utcnow().isoformat()
    }
    emit("chat_message", msg, room=room)

# helper notify
def notify_new_post(post):
    poster = post.user
    follower_ids = [f.id for f in poster.followers]
    for fid in follower_ids:
        create_notification(target_user_id=fid,
                            actor_id=poster.id,
                            typ="new_post",
                            message=f"{poster.fullname} published a new post",
                            data_obj={"post_id": post.id},
                            emit_real_time=True)
	
	# --- Notifications: list & mark read ---
@app.route("/api/notifications", methods=["GET"])
@login_required
def list_notifications():
    uid = session["user_id"]
    page = max(int(request.args.get("page") or 1), 1)
    per_page = min(int(request.args.get("per_page") or 25), 200)
    q = Notification.query.filter_by(user_id=uid).order_by(Notification.created_at.desc())
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    items = [n.to_dict() for n in pagination.items]
    return jsonify({"notifications": items, "page": page, "per_page": per_page, "total": pagination.total, "pages": pagination.pages})

@app.route("/api/notifications/mark_read", methods=["POST"])
@login_required
def mark_notifications_read():
    uid = session["user_id"]
    ids = request.json.get("ids") if request.is_json else request.form.getlist("ids")
    if not ids:
        return jsonify({"error": "ids required"}), 400
    # convert to ints
    try:
        ids = [int(i) for i in ids]
    except Exception:
        return jsonify({"error": "invalid_ids"}), 400
    Notification.query.filter(Notification.user_id == uid, Notification.id.in_(ids)).update({"read": True}, synchronize_session=False)
    db.session.commit()
    return jsonify({"marked": ids})


# ---------------- Boot / Initialization ----------------
def create_default_developer_if_empty():
    """
    If the DB has no users, create a developer account for initial setup.
    Prints credentials to console so you can log in and promote others.
    """
    try:
        if User.query.count() == 0:
            pw = "devpass"  # change later
            dev = User(fullname="Developer Account", email="developer@local", password_hash=generate_password_hash(pw), role=ROLE_DEVELOPER)
            db.session.add(dev)
            db.session.commit()
            print("=== Created default developer account ===")
            print("email: developer@local")
            print("password:", pw)
            print("Please change or delete this account after first login.")
    except Exception as e:
        print("create_default_developer_if_empty error:", e)

if __name__ == "__main__":
    with app.app_context():
        # create tables if not exist
        db.create_all()
        # ensure extra user columns exist in SQLite
        ensure_user_columns()
        # create default dev if DB empty (useful for first-run)
        create_default_developer_if_empty()

    # run via socketio for realtime
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
	# Admin Users page (render HTML)
@app.route("/admin/users")
@role_required([ROLE_ADMIN, ROLE_DEVELOPER])
def admin_users_page():
    return render_template("admin_users.html")

# API for admin to list users (for the UI)
@app.route("/api/admin/users", methods=["GET"])
@role_required([ROLE_ADMIN, ROLE_DEVELOPER])
def admin_list_users():
    page = max(int(request.args.get("page") or 1), 1)
    per_page = min(int(request.args.get("per_page") or 50), 200)
    q = User.query.order_by(User.created_at.desc())
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    items = [u.to_dict(public=True) for u in pagination.items]
    return jsonify({"users": items, "page": page, "per_page": per_page, "total": pagination.total, "pages": pagination.pages})

