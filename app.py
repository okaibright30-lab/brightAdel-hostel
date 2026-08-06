import os
import io
import csv
import random
import uuid
import hashlib
import hmac
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps

import requests
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    abort,
    session,
    Response,
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename


basedir = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "brightadel-dev-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(basedir, "brightadel.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = os.path.join(basedir, "static", "uploads")

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "warning"


# -----------------------------
# CONFIGURATION (environment-driven for production)
# -----------------------------
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")

# SMS: leave SMS_API_KEY empty for demo mode.
# Providers supported: arkesel | termii | mnotify
SMS_API_KEY = os.environ.get("SMS_API_KEY", "")
SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "arkesel")
SMS_SENDER_ID = os.environ.get("SMS_SENDER_ID", "BrightAdel")


HOSTEL_LOCATIONS = ["Ayensu", "Kwaprow", "Amamoma", "Old Site"]
AMENITY_OPTIONS = ["water", "electricity", "wifi"]
PHOTO_LABELS = ["Cover (Compound)", "Bathroom", "Bedroom", "Kitchen"]
ROOM_TYPE_CAPACITY = {
    "1 in a room": 1,
    "2 in a room": 2,
    "3 in a room": 3,
    "4 in a room": 4,
}
BOOKING_STATUSES = ["pending", "confirmed", "checked_in", "checkout_requested", "checked_out", "cancelled"]
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "pdf"}

PLATFORM_FEE = Decimal("20.00")
PROCESSING_FEE_RATE = Decimal("0.02")


def utcnow():
    return datetime.now(timezone.utc)


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


def processing_fee(amount):
    return money(Decimal(str(amount)) * PROCESSING_FEE_RATE)


def extra_fees(amount):
    return money(PLATFORM_FEE + processing_fee(amount))


def split_charge(charge):
    base = money((Decimal(str(charge)) - PLATFORM_FEE) / (Decimal("1") + PROCESSING_FEE_RATE))
    for candidate in (base, base + Decimal("0.01"), base - Decimal("0.01")):
        if candidate >= 0 and candidate + extra_fees(candidate) == money(charge):
            return candidate
    return base


def normalize_phone(value):
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if digits.startswith("233") and len(digits) == 12:
        digits = "0" + digits[3:]
    return digits


def valid_phone(digits):
    return digits.startswith("0") and len(digits) == 10


def to_international(phone):
    if phone.startswith("0"):
        return "233" + phone[1:]
    if phone.startswith("+"):
        return phone[1:]
    return phone


def send_sms(phone, message):
    """Send SMS via a real provider when SMS_API_KEY is set; otherwise demo mode."""
    if not SMS_API_KEY:
        print(f"[SMS DEMO] To {phone}: {message}")
        return True

    intl = to_international(phone)

    try:
        if SMS_PROVIDER == "termii":
            resp = requests.post(
                "https://api.ng.termii.com/api/v1/sms/send",
                json={
                    "api_key": SMS_API_KEY,
                    "to": intl,
                    "from": SMS_SENDER_ID,
                    "sms": message,
                    "type": "plain",
                    "channel": "generic",
                },
                timeout=20,
            )
        elif SMS_PROVIDER == "mnotify":
            resp = requests.post(
                "https://apps.mnotify.net/smsapi/one/batch/",
                data={
                    "api_key": SMS_API_KEY,
                    "sender_id": SMS_SENDER_ID,
                    "numbers": intl,
                    "message": message,
                },
                timeout=20,
            )
        else:  # arkesel (default)
            resp = requests.get(
                "https://sms.arkesel.com/sms/api",
                params={
                    "action": "send-sms",
                    "api-key": SMS_API_KEY,
                    "from": SMS_SENDER_ID,
                    "to": intl,
                    "message": message,
                },
                timeout=20,
            )

        print(f"[SMS via {SMS_PROVIDER}] {resp.status_code}: {resp.text[:200]}")
        return resp.status_code == 200
    except Exception as e:
        print(f"[SMS ERROR] {e}")
        return False


def receipt_code(booking):
    secret = app.config["SECRET_KEY"]
    digest = hmac.new(secret.encode(), f"BA-{booking.id}".encode(), hashlib.sha256).hexdigest()
    return digest[:8].upper()


def notify(user_id, message):
    db.session.add(Notification(user_id=user_id, message=message))


# -----------------------------
# Models
# -----------------------------

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), nullable=False)
    first_name = db.Column(db.String(60), nullable=False)
    last_name = db.Column(db.String(60), nullable=False)
    phone = db.Column(db.String(15), unique=True, nullable=False)
    email = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="student")
    suspended = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    hostels = db.relationship("Hostel", backref="manager", lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class ManagerApplication(db.Model):
    __tablename__ = "manager_applications"

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(60), nullable=False)
    last_name = db.Column(db.String(60), nullable=False)
    phone = db.Column(db.String(15), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    hostel_name = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(50), nullable=False)
    doc_filename = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    created_at = db.Column(db.DateTime, default=utcnow)


class Hostel(db.Model):
    __tablename__ = "hostels"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(50), nullable=False)
    paid_amenities = db.Column(db.String(200), nullable=True, default="")
    has_ac = db.Column(db.Boolean, default=False, nullable=False)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    rooms = db.relationship("Room", backref="hostel", lazy=True)
    photos = db.relationship("HostelPhoto", backref="hostel", lazy=True)

    @property
    def paid_amenities_list(self):
        return [a for a in (self.paid_amenities or "").split(",") if a]

    @property
    def has_map(self):
        return self.latitude is not None and self.longitude is not None

    def average_rating(self):
        reviews = Review.query.filter_by(hostel_id=self.id).all()
        if not reviews:
            return None
        return round(sum(r.rating for r in reviews) / len(reviews), 1)

    def review_count(self):
        return Review.query.filter_by(hostel_id=self.id).count()


class HostelPhoto(db.Model):
    __tablename__ = "hostel_photos"

    id = db.Column(db.Integer, primary_key=True)
    hostel_id = db.Column(db.Integer, db.ForeignKey("hostels.id"), nullable=False)
    label = db.Column(db.String(50), nullable=False)
    filename = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)


class Room(db.Model):
    __tablename__ = "rooms"

    id = db.Column(db.Integer, primary_key=True)
    hostel_id = db.Column(db.Integer, db.ForeignKey("hostels.id"), nullable=False)
    room_type = db.Column(db.String(30), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    capacity = db.Column(db.Integer, nullable=False, default=1)
    price_per_year = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)

    bookings = db.relationship("Booking", backref="room", lazy=True)

    @property
    def total_slots(self):
        return self.quantity * self.capacity

    def booked_slots(self):
        return Booking.query.filter(
            Booking.room_id == self.id,
            Booking.status.in_(["confirmed", "checked_in", "checkout_requested"]),
        ).count()

    def available_slots(self):
        return self.total_slots - self.booked_slots()


class Booking(db.Model):
    __tablename__ = "bookings"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey("rooms.id"), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="pending")
    total_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    student = db.relationship("User", backref="bookings")
    payments = db.relationship("Payment", backref="booking", lazy=True)

    def paid_amount(self):
        value = db.session.query(
            db.func.coalesce(db.func.sum(Payment.amount), 0)
        ).filter(
            Payment.booking_id == self.id,
            Payment.status == "success",
        ).scalar()
        return Decimal(str(value))

    def balance(self):
        return Decimal(str(self.total_amount)) - self.paid_amount()

    def half_fee(self):
        return Decimal(str(self.total_amount)) / Decimal("2")

    def review(self):
        return Review.query.filter_by(booking_id=self.id).first()


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    method = db.Column(db.String(50), nullable=False, default="paystack")
    status = db.Column(db.String(20), nullable=False, default="success")
    reference = db.Column(db.String(100), unique=True, nullable=False)
    paid_at = db.Column(db.DateTime, default=utcnow)


class Review(db.Model):
    __tablename__ = "reviews"

    id = db.Column(db.Integer, primary_key=True)
    hostel_id = db.Column(db.Integer, db.ForeignKey("hostels.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    student = db.relationship("User")
    hostel = db.relationship("Hostel", backref="reviews")


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    user = db.relationship("User", backref="notifications")


class Announcement(db.Model):
    __tablename__ = "announcements"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)


# -----------------------------
# Login loader + helpers
# -----------------------------

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@app.context_processor
def inject_unread_count():
    if current_user.is_authenticated:
        count = Notification.query.filter_by(user_id=current_user.id, read=False).count()
        return dict(unread_count=count)
    return dict(unread_count=0)


def role_required(*roles):
    def wrapper(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("login", next=request.url))
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return wrapper


def manager_owns_hostel(hostel):
    return current_user.role == "admin" or hostel.manager_id == current_user.id


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def parse_coordinates(lat_raw, lng_raw):
    lat_raw = (lat_raw or "").strip()
    lng_raw = (lng_raw or "").strip()

    if not lat_raw and not lng_raw:
        return None, None, None

    try:
        latitude = float(lat_raw)
        longitude = float(lng_raw)
        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            raise ValueError
        return latitude, longitude, None
    except (ValueError, TypeError):
        return None, None, "Coordinates look invalid. Use the GPS button or leave both empty."


def upgrade_database():
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)

    if "hostels" in inspector.get_table_names():
        cols = [c["name"] for c in inspector.get_columns("hostels")]
        with db.engine.connect() as conn:
            if "latitude" not in cols:
                conn.execute(text("ALTER TABLE hostels ADD COLUMN latitude FLOAT"))
            if "longitude" not in cols:
                conn.execute(text("ALTER TABLE hostels ADD COLUMN longitude FLOAT"))
            conn.commit()


# -----------------------------
# Error pages
# -----------------------------

@app.errorhandler(404)
def error_404(e):
    return render_template("error.html", code=404, message="The page you are looking for does not exist."), 404


@app.errorhandler(403)
def error_403(e):
    return render_template("error.html", code=403, message="You do not have permission to view this page."), 403


@app.errorhandler(500)
def error_500(e):
    return render_template("error.html", code=500, message="Something went wrong on our side. Please try again."), 500


# -----------------------------
# Public pages
# -----------------------------

@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    location = request.args.get("location", "").strip()
    room_type = request.args.get("room_type", "").strip()
    ac = request.args.get("ac") == "on"
    has_slots = request.args.get("slots") == "on"
    included_amenities = [a for a in request.args.getlist("amenities") if a in AMENITY_OPTIONS]
    sort = request.args.get("sort", "name")

    min_price = None
    max_price = None
    try:
        if request.args.get("min_price"):
            min_price = Decimal(request.args.get("min_price"))
        if request.args.get("max_price"):
            max_price = Decimal(request.args.get("max_price"))
    except Exception:
        min_price = None
        max_price = None

    query = Hostel.query

    if q:
        query = query.filter(Hostel.name.ilike(f"%{q}%"))

    if location:
        query = query.filter(Hostel.location == location)

    cards = []
    for hostel in query.all():
        rooms = list(hostel.rooms)

        if room_type:
            rooms = [r for r in rooms if r.room_type == room_type]
            if not rooms:
                continue

        if ac and not hostel.has_ac:
            continue

        skip = False
        for amenity in included_amenities:
            if amenity in hostel.paid_amenities_list:
                skip = True
                break
        if skip:
            continue

        total_slots = sum(r.total_slots for r in rooms)
        available_slots = sum(r.available_slots() for r in rooms)
        prices = [r.price_per_year for r in rooms]
        cheapest = min(prices, default=None)

        if has_slots and available_slots <= 0:
            continue

        if max_price is not None and (cheapest is None or cheapest > max_price):
            continue
        if min_price is not None and (cheapest is None or cheapest < min_price):
            continue

        cover = HostelPhoto.query.filter_by(hostel_id=hostel.id, label="Cover (Compound)").first()
        if not cover:
            cover = HostelPhoto.query.filter_by(hostel_id=hostel.id).first()

        cards.append({
            "hostel": hostel,
            "total_slots": total_slots,
            "available_slots": available_slots,
            "min_price": cheapest,
            "cover": cover,
            "rating": hostel.average_rating(),
            "review_count": hostel.review_count(),
        })

    if sort == "price_asc":
        cards.sort(key=lambda c: c["min_price"] if c["min_price"] is not None else Decimal("999999999"))
    elif sort == "price_desc":
        cards.sort(key=lambda c: c["min_price"] if c["min_price"] is not None else Decimal("0"), reverse=True)
    elif sort == "rating":
        cards.sort(key=lambda c: (c["rating"] if c["rating"] is not None else -1, c["review_count"]), reverse=True)
    else:
        cards.sort(key=lambda c: c["hostel"].name)

    announcements = Announcement.query.order_by(Announcement.created_at.desc()).limit(3).all()

    return render_template(
        "index.html",
        cards=cards,
        announcements=announcements,
        q=q,
        location=location,
        room_type=room_type,
        ac=ac,
        has_slots=has_slots,
        included_amenities=included_amenities,
        sort=sort,
        min_price_str=request.args.get("min_price", ""),
        max_price_str=request.args.get("max_price", ""),
        locations=HOSTEL_LOCATIONS,
        room_types=list(ROOM_TYPE_CAPACITY.keys()),
        amenities=AMENITY_OPTIONS,
    )


@app.route("/hostels/<int:hostel_id>")
def hostel_detail(hostel_id):
    hostel = db.session.get(Hostel, hostel_id) or abort(404)
    rooms = sorted(hostel.rooms, key=lambda r: r.room_type)
    photos = hostel.photos
    reviews = Review.query.filter_by(hostel_id=hostel.id).order_by(Review.created_at.desc()).all()
    return render_template("hostel_detail.html", hostel=hostel, rooms=rooms, photos=photos, reviews=reviews)


@app.route("/about")
def about():
    return render_template("about.html")


# -----------------------------
# Notifications
# -----------------------------

@app.route("/notifications")
@login_required
def notifications():
    items = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(50).all()
    return render_template("notifications.html", items=items)


@app.route("/notifications/read", methods=["POST"])
@login_required
def notifications_read():
    Notification.query.filter_by(user_id=current_user.id, read=False).update({"read": True})
    db.session.commit()
    flash("All notifications marked as read.", "success")
    return redirect(url_for("notifications"))


# -----------------------------
# Authentication + profile + password reset
# -----------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        phone = normalize_phone(request.form.get("phone", ""))
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        role = request.form.get("role", "student")

        error = None

        if not first_name or not last_name or not phone or not password:
            error = "All fields are required."
        elif not valid_phone(phone):
            error = "Enter a valid 10-digit phone number, e.g. 0244123456."
        elif password != confirm_password:
            error = "Passwords do not match."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif role not in ["student", "manager"]:
            error = "Invalid account type."
        elif User.query.filter_by(phone=phone).first():
            error = "This phone number is already registered."

        if error:
            flash(error, "danger")
            return render_template("register.html", locations=HOSTEL_LOCATIONS)

        if role == "manager":
            hostel_name = request.form.get("hostel_name", "").strip()
            location = request.form.get("location", "")
            file = request.files.get("document")

            if not hostel_name:
                flash("Please enter the name of the hostel you own.", "danger")
            elif location not in HOSTEL_LOCATIONS:
                flash("Please choose the hostel location.", "danger")
            elif not file or file.filename == "":
                flash("Please upload a proof-of-ownership document.", "danger")
            elif not allowed_file(file.filename):
                flash("Document must be JPG, PNG, WEBP or PDF.", "danger")
            elif ManagerApplication.query.filter_by(phone=phone, status="pending").first():
                flash("You already have an application under review.", "warning")
            else:
                filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
                file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

                application = ManagerApplication(
                    first_name=first_name,
                    last_name=last_name,
                    phone=phone,
                    password_hash=generate_password_hash(password),
                    hostel_name=hostel_name,
                    location=location,
                    doc_filename=filename,
                    status="pending",
                )
                db.session.add(application)

                for admin in User.query.filter_by(role="admin").all():
                    notify(admin.id, f"New manager application from {first_name} {last_name} ({phone}).")

                db.session.commit()

                flash("Application submitted! The admin will review your ownership document. You can log in once approved.", "success")
                return redirect(url_for("login"))

            return render_template("register.html", locations=HOSTEL_LOCATIONS)

        code = f"{random.randint(0, 999999):06d}"

        session["reg_data"] = {
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
            "password_hash": generate_password_hash(password),
            "role": "student",
        }
        session["reg_code"] = code
        session["reg_time"] = int(datetime.now().timestamp())

        send_sms(phone, f"BrightAdel verification code: {code}")

        flash("We sent a 6-digit verification code to your phone via SMS.", "info")
        return redirect(url_for("verify"))

    return render_template("register.html", locations=HOSTEL_LOCATIONS)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    data = session.get("reg_data")

    if not data:
        return redirect(url_for("register"))

    if request.method == "POST":
        entered = request.form.get("code", "").strip()
        age_minutes = (int(datetime.now().timestamp()) - session.get("reg_time", 0)) / 60

        if age_minutes > 10:
            flash("Code expired. Please resend a new code.", "warning")
        elif entered == session.get("reg_code"):
            phone = data["phone"]

            if User.query.filter_by(phone=phone).first():
                session.pop("reg_data", None)
                session.pop("reg_code", None)
                session.pop("reg_time", None)
                flash("This phone number is already registered.", "danger")
                return redirect(url_for("login"))

            user = User(
                username=f"{data['first_name']} {data['last_name']}",
                first_name=data["first_name"],
                last_name=data["last_name"],
                phone=phone,
                email=f"{phone}@brightadel.com",
                role="student",
            )
            user.password_hash = data["password_hash"]

            db.session.add(user)
            db.session.commit()

            session.pop("reg_data", None)
            session.pop("reg_code", None)
            session.pop("reg_time", None)

            flash("Phone verified! Account created. You can now log in.", "success")
            return redirect(url_for("login"))
        else:
            flash("Incorrect code. Try again.", "danger")

    return render_template(
        "verify.html",
        phone=data["phone"],
        test_code=session.get("reg_code") if not SMS_API_KEY else None,
    )


@app.route("/verify/resend", methods=["POST"])
def verify_resend():
    data = session.get("reg_data")

    if not data:
        return redirect(url_for("register"))

    code = f"{random.randint(0, 999999):06d}"
    session["reg_code"] = code
    session["reg_time"] = int(datetime.now().timestamp())
    send_sms(data["phone"], f"BrightAdel verification code: {code}")

    flash("A new SMS code has been sent.", "info")
    return redirect(url_for("verify"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        phone = normalize_phone(request.form.get("phone", ""))
        password = request.form.get("password", "")

        user = User.query.filter_by(phone=phone).first()

        if user and user.check_password(password):
            if user.suspended:
                flash("This account has been suspended. Contact support.", "danger")
            else:
                login_user(user)
                flash(f"Welcome back, {user.username}!", "success")
                return redirect(url_for("dashboard"))
        else:
            pending = ManagerApplication.query.filter_by(phone=phone, status="pending").first()

            if pending:
                flash("Your application to list a hostel is still under review.", "warning")
            else:
                flash("Invalid phone number or password.", "danger")

    return render_template("login.html")


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        phone = normalize_phone(request.form.get("phone", ""))
        user = User.query.filter_by(phone=phone).first()

        if not user:
            flash("No account found with this phone number.", "danger")
        else:
            code = f"{random.randint(0, 999999):06d}"
            session["forgot_phone"] = phone
            session["forgot_code"] = code
            session["forgot_time"] = int(datetime.now().timestamp())
            send_sms(phone, f"BrightAdel password reset code: {code}")
            flash("We sent a 6-digit reset code to your phone.", "info")
            return redirect(url_for("reset"))

    return render_template("forgot.html")


@app.route("/reset", methods=["GET", "POST"])
def reset():
    phone = session.get("forgot_phone")

    if not phone:
        return redirect(url_for("forgot"))

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        age_minutes = (int(datetime.now().timestamp()) - session.get("forgot_time", 0)) / 60

        user = User.query.filter_by(phone=phone).first()

        if age_minutes > 10:
            flash("Code expired. Please request a new one.", "warning")
            return redirect(url_for("forgot"))
        elif code != session.get("forgot_code"):
            flash("Incorrect code.", "danger")
        elif not user:
            flash("Account not found.", "danger")
        elif len(new_password) < 6:
            flash("Password must be at least 6 characters.", "danger")
        elif new_password != confirm_password:
            flash("Passwords do not match.", "danger")
        else:
            user.set_password(new_password)
            db.session.commit()
            session.pop("forgot_phone", None)
            session.pop("forgot_code", None)
            session.pop("forgot_time", None)
            flash("Password reset successful. You can now log in.", "success")
            return redirect(url_for("login"))

    return render_template(
        "reset.html",
        phone=phone,
        test_code=session.get("forgot_code") if not SMS_API_KEY else None,
    )


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    if current_user.role == "admin":
        return redirect(url_for("admin_dashboard"))
    if current_user.role == "manager":
        return redirect(url_for("manager_dashboard"))
    return redirect(url_for("my_bookings"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        action = request.form.get("action", "update_info")

        if action == "update_info":
            first_name = request.form.get("first_name", "").strip()
            last_name = request.form.get("last_name", "").strip()

            if not first_name or not last_name:
                flash("Names cannot be empty.", "danger")
            else:
                current_user.first_name = first_name
                current_user.last_name = last_name
                current_user.username = f"{first_name} {last_name}"
                db.session.commit()
                flash("Profile updated successfully.", "success")

        elif action == "change_password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not current_user.check_password(current_password):
                flash("Current password is incorrect.", "danger")
            elif len(new_password) < 6:
                flash("New password must be at least 6 characters.", "danger")
            elif new_password != confirm_password:
                flash("New passwords do not match.", "danger")
            else:
                current_user.set_password(new_password)
                db.session.commit()
                flash("Password changed successfully.", "success")

        return redirect(url_for("profile"))

    return render_template("profile.html", user=current_user)


# -----------------------------
# Student: bookings, payment, receipt, checkout, reviews
# -----------------------------

@app.route("/my/bookings")
@role_required("student")
def my_bookings():
    bookings = Booking.query.filter_by(student_id=current_user.id).order_by(Booking.created_at.desc()).all()
    return render_template("my_bookings.html", bookings=bookings)


@app.route("/book/room/<int:room_id>", methods=["GET", "POST"])
@role_required("student")
def booking_new(room_id):
    room = db.session.get(Room, room_id) or abort(404)

    if request.method == "POST":
        notes = request.form.get("notes", "").strip()

        if room.available_slots() <= 0:
            flash("No slots left in this room type.", "danger")
        else:
            booking = Booking(
                student_id=current_user.id,
                room_id=room.id,
                status="pending",
                total_amount=room.price_per_year,
                notes=notes,
            )
            db.session.add(booking)

            notify(room.hostel.manager_id, f"New booking: {current_user.username} booked {room.room_type} at {room.hostel.name}.")

            db.session.commit()

            flash("Booking created. Pay at least half to confirm your slot.", "success")
            return redirect(url_for("booking_pay", booking_id=booking.id))

    return render_template("booking_form.html", room=room)


@app.route("/bookings/<int:booking_id>/pay", methods=["GET", "POST"])
@role_required("student")
def booking_pay(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    if booking.student_id != current_user.id:
        abort(403)

    if booking.status not in ["pending", "confirmed"]:
        flash("This booking cannot be paid.", "warning")
        return redirect(url_for("my_bookings"))

    if request.method == "POST":
        if booking.status == "pending" and booking.room.available_slots() <= 0:
            flash("No slots left in this room type. Payment not processed.", "danger")
            return redirect(url_for("my_bookings"))

        option = request.form.get("pay_option", "")
        paid = booking.paid_amount()
        balance = booking.balance()

        if option == "half" and paid == 0:
            amount = booking.half_fee()
        elif option in ["full", "balance"]:
            amount = balance
        else:
            flash("Invalid payment option.", "danger")
            return redirect(url_for("booking_pay", booking_id=booking.id))

        if amount <= 0:
            flash("Nothing left to pay. Your booking is fully paid.", "info")
            return redirect(url_for("booking_receipt", booking_id=booking.id))

        fee = extra_fees(amount)
        charge = money(amount + fee)

        if PAYSTACK_SECRET_KEY:
            callback_url = url_for("payment_callback", booking_id=booking.id, _external=True)

            payload = {
                "email": current_user.email,
                "amount": int(charge * 100),
                "callback_url": callback_url,
                "metadata": {
                    "booking_id": booking.id,
                    "student": current_user.username,
                },
            }
            headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}

            try:
                resp = requests.post(
                    "https://api.paystack.co/transaction/initialize",
                    json=payload,
                    headers=headers,
                    timeout=30,
                )
                data = resp.json()
            except Exception:
                flash("Could not reach Paystack. Check your internet connection.", "danger")
                return redirect(url_for("booking_pay", booking_id=booking.id))

            if not data.get("status"):
                flash(f"Paystack error: {data.get('message', 'Unknown error')}", "danger")
                return redirect(url_for("booking_pay", booking_id=booking.id))

            session["pending_reference"] = data["data"]["reference"]
            session["pay_plan"] = {
                "booking": booking.id,
                "base": str(amount),
                "charge": str(charge),
            }
            return redirect(data["data"]["authorization_url"])
        else:
            payment = Payment(
                booking_id=booking.id,
                amount=amount,
                method="paystack-test",
                status="success",
                reference=f"BA-{booking.id}-{int(datetime.now().timestamp())}",
            )
            db.session.add(payment)

            if booking.status == "pending" and (paid + amount) >= booking.half_fee():
                booking.status = "confirmed"

            notify(current_user.id, f"Payment of GH₵ {amount} received for {booking.room.hostel.name}. Receipt code: {receipt_code(booking)}.")
            notify(booking.room.hostel.manager_id, f"Payment received: {current_user.username} paid GH₵ {amount} for {booking.room.hostel.name}.")

            db.session.commit()

            flash("Payment successful!", "success")
            return redirect(url_for("booking_receipt", booking_id=booking.id))

    return render_template(
        "pay.html",
        booking=booking,
        paystack_enabled=bool(PAYSTACK_SECRET_KEY),
    )


@app.route("/payment/callback/<int:booking_id>")
@role_required("student")
def payment_callback(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    if booking.student_id != current_user.id:
        abort(403)

    reference = (
        request.args.get("reference")
        or request.args.get("trxref")
        or session.pop("pending_reference", None)
    )

    if not reference:
        flash("Payment reference missing.", "danger")
        return redirect(url_for("my_bookings"))

    if Payment.query.filter_by(reference=reference).first():
        flash("Payment already recorded.", "info")
        return redirect(url_for("booking_receipt", booking_id=booking.id))

    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}

    try:
        resp = requests.get(
            f"https://api.paystack.co/transaction/verify/{reference}",
            headers=headers,
            timeout=30,
        )
        data = resp.json()
    except Exception:
        flash("Could not verify payment. Contact support with your reference.", "danger")
        return redirect(url_for("my_bookings"))

    if data.get("status") and data["data"]["status"] == "success":
        charged = Decimal(str(data["data"]["amount"])) / Decimal("100")

        plan = session.pop("pay_plan", None)
        base = None
        if plan and plan.get("booking") == booking.id and abs(Decimal(plan["charge"]) - charged) < Decimal("0.02"):
            base = Decimal(plan["base"])
        else:
            base = split_charge(charged)

        payment = Payment(
            booking_id=booking.id,
            amount=base,
            method="paystack",
            status="success",
            reference=reference,
        )
        db.session.add(payment)

        if booking.status == "pending" and (booking.paid_amount() + base) >= booking.half_fee():
            booking.status = "confirmed"

        notify(current_user.id, f"Payment of GH₵ {base} received for {booking.room.hostel.name}. Receipt code: {receipt_code(booking)}.")
        notify(booking.room.hostel.manager_id, f"Payment received: {current_user.username} paid GH₵ {base} for {booking.room.hostel.name}.")

        db.session.commit()

        flash("Payment confirmed by Paystack. Your slot is secured!", "success")
        return redirect(url_for("booking_receipt", booking_id=booking.id))

    flash("Payment was not successful. Please try again.", "warning")
    return redirect(url_for("booking_pay", booking_id=booking.id))


@app.route("/bookings/<int:booking_id>/receipt")
@role_required("student", "manager", "admin")
def booking_receipt(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    if current_user.role == "student" and booking.student_id != current_user.id:
        abort(403)
    if current_user.role == "manager" and not manager_owns_hostel(booking.room.hostel):
        abort(403)

    return render_template("receipt.html", booking=booking, code=receipt_code(booking))


@app.route("/bookings/<int:booking_id>/cancel", methods=["POST"])
@role_required("student")
def booking_cancel(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    if booking.student_id != current_user.id:
        abort(403)

    if booking.status != "pending":
        flash("Only unpaid bookings can be cancelled.", "warning")
    else:
        booking.status = "cancelled"
        db.session.commit()
        flash("Booking cancelled.", "info")

    return redirect(url_for("my_bookings"))


@app.route("/bookings/<int:booking_id>/leave", methods=["POST"])
@role_required("student")
def booking_leave(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    if booking.student_id != current_user.id:
        abort(403)

    if booking.status not in ["confirmed", "checked_in"]:
        flash("You can only leave an active (paid) booking.", "warning")
    else:
        booking.status = "checked_out"
        db.session.commit()
        flash("You checked out. Your slot is free and you can book another hostel.", "success")

    return redirect(url_for("my_bookings"))


@app.route("/bookings/<int:booking_id>/approve-checkout", methods=["POST"])
@role_required("student")
def booking_approve_checkout(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    if booking.student_id != current_user.id:
        abort(403)

    if booking.status != "checkout_requested":
        flash("There is no check-out request on this booking.", "warning")
    else:
        booking.status = "checked_out"
        notify(booking.room.hostel.manager_id, f"{current_user.username} approved the check-out at {booking.room.hostel.name}.")
        db.session.commit()
        flash("You approved the check-out. Your slot has been freed.", "success")

    return redirect(url_for("my_bookings"))


@app.route("/bookings/<int:booking_id>/decline-checkout", methods=["POST"])
@role_required("student")
def booking_decline_checkout(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    if booking.student_id != current_user.id:
        abort(403)

    if booking.status != "checkout_requested":
        flash("There is no check-out request on this booking.", "warning")
    else:
        booking.status = "checked_in"
        notify(booking.room.hostel.manager_id, f"{current_user.username} declined the check-out request at {booking.room.hostel.name}.")
        db.session.commit()
        flash("You declined the check-out request. You remain in the hostel.", "info")

    return redirect(url_for("my_bookings"))


@app.route("/bookings/<int:booking_id>/review", methods=["GET", "POST"])
@role_required("student")
def review_new(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    if booking.student_id != current_user.id:
        abort(403)

    if booking.status != "checked_out":
        flash("You can review a hostel only after checking out.", "warning")
        return redirect(url_for("my_bookings"))

    if booking.review():
        flash("You have already reviewed this hostel.", "info")
        return redirect(url_for("my_bookings"))

    if request.method == "POST":
        rating = request.form.get("rating", type=int)
        comment = request.form.get("comment", "").strip()

        if not rating or rating < 1 or rating > 5:
            flash("Please choose a star rating from 1 to 5.", "danger")
        else:
            review = Review(
                hostel_id=booking.room.hostel_id,
                student_id=current_user.id,
                booking_id=booking.id,
                rating=rating,
                comment=comment,
            )
            db.session.add(review)

            notify(booking.room.hostel.manager_id, f"New review: {current_user.username} rated {booking.room.hostel.name} {rating}/5.")

            db.session.commit()
            flash("Thank you! Your review is now live.", "success")
            return redirect(url_for("hostel_detail", hostel_id=booking.room.hostel_id))

    return render_template("review_form.html", booking=booking)


# -----------------------------
# Manager: hostels, rooms, photos, bookings, receipts, export
# -----------------------------

@app.route("/manager")
@role_required("manager", "admin")
def manager_dashboard():
    if current_user.role == "admin":
        hostels = Hostel.query.all()
    else:
        hostels = Hostel.query.filter_by(manager_id=current_user.id).all()

    total_slots = 0
    booked_slots = 0
    for hostel in hostels:
        for room in hostel.rooms:
            total_slots += room.total_slots
            booked_slots += room.booked_slots()

    bookings = []
    for hostel in hostels:
        for room in hostel.rooms:
            bookings.extend(room.bookings)
    bookings.sort(key=lambda b: b.created_at, reverse=True)

    return render_template(
        "manager_dashboard.html",
        hostels=hostels,
        total_slots=total_slots,
        booked_slots=booked_slots,
        available_slots=total_slots - booked_slots,
        bookings=bookings,
    )


@app.route("/export/bookings")
@role_required("manager", "admin")
def export_bookings():
    if current_user.role == "admin":
        bookings = Booking.query.order_by(Booking.created_at.desc()).all()
    else:
        hostel_ids = [h.id for h in Hostel.query.filter_by(manager_id=current_user.id).all()]
        bookings = Booking.query.join(Room).filter(Room.hostel_id.in_(hostel_ids)).order_by(Booking.created_at.desc()).all() if hostel_ids else []

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Student", "Phone", "Hostel", "Room Type", "Status", "Paid (GHS)", "Total (GHS)", "Date"])

    for b in bookings:
        writer.writerow([
            b.student.username,
            b.student.phone,
            b.room.hostel.name,
            b.room.room_type,
            b.status,
            str(b.paid_amount()),
            str(b.total_amount),
            b.created_at.strftime("%d %b %Y"),
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=brightadel_bookings.csv"},
    )


@app.route("/manager/receipts")
@role_required("manager", "admin")
def manager_receipts():
    if current_user.role == "admin":
        bookings = Booking.query.all()
    else:
        hostel_ids = [h.id for h in Hostel.query.filter_by(manager_id=current_user.id).all()]
        bookings = Booking.query.join(Room).filter(Room.hostel_id.in_(hostel_ids)).all() if hostel_ids else []

    paid = [b for b in bookings if b.paid_amount() > 0]
    paid.sort(key=lambda b: b.created_at, reverse=True)

    return render_template("manager_receipts.html", bookings=paid)


@app.route("/manager/verify-receipt", methods=["GET", "POST"])
@role_required("manager", "admin")
def verify_receipt():
    result = None
    result_code = ""

    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()

        for booking in Booking.query.all():
            if receipt_code(booking) == code:
                result = booking
                result_code = code
                break

        if not result:
            flash("No receipt matches this code. It may be FAKE.", "danger")

    return render_template("verify_receipt.html", result=result, result_code=result_code)


@app.route("/manager/hostels/new", methods=["GET", "POST"])
@role_required("manager")
def hostel_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        location = request.form.get("location", "")
        paid_amenities = request.form.getlist("paid_amenities")
        has_ac = request.form.get("has_ac") == "on"
        latitude, longitude, coord_error = parse_coordinates(
            request.form.get("latitude"),
            request.form.get("longitude"),
        )

        if not name:
            flash("Hostel name is required.", "danger")
        elif location not in HOSTEL_LOCATIONS:
            flash("Please choose a valid location.", "danger")
        elif coord_error:
            flash(coord_error, "danger")
        else:
            hostel = Hostel(
                name=name,
                location=location,
                paid_amenities=",".join([a for a in paid_amenities if a in AMENITY_OPTIONS]),
                has_ac=has_ac,
                latitude=latitude,
                longitude=longitude,
                manager_id=current_user.id,
            )
            db.session.add(hostel)
            db.session.commit()
            flash("Hostel created. Now add rooms and photos.", "success")
            return redirect(url_for("hostel_edit", hostel_id=hostel.id))

    return render_template(
        "hostel_form.html",
        hostel=None,
        locations=HOSTEL_LOCATIONS,
        amenities=AMENITY_OPTIONS,
        photo_labels=PHOTO_LABELS,
    )


@app.route("/manager/hostels/<int:hostel_id>/edit", methods=["GET", "POST"])
@role_required("manager")
def hostel_edit(hostel_id):
    hostel = db.session.get(Hostel, hostel_id) or abort(404)

    if not manager_owns_hostel(hostel):
        abort(403)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        location = request.form.get("location", "")
        paid_amenities = request.form.getlist("paid_amenities")
        has_ac = request.form.get("has_ac") == "on"
        latitude, longitude, coord_error = parse_coordinates(
            request.form.get("latitude"),
            request.form.get("longitude"),
        )

        if not name:
            flash("Hostel name is required.", "danger")
        elif location not in HOSTEL_LOCATIONS:
            flash("Please choose a valid location.", "danger")
        elif coord_error:
            flash(coord_error, "danger")
        else:
            hostel.name = name
            hostel.location = location
            hostel.paid_amenities = ",".join([a for a in paid_amenities if a in AMENITY_OPTIONS])
            hostel.has_ac = has_ac
            hostel.latitude = latitude
            hostel.longitude = longitude
            db.session.commit()
            flash("Hostel updated successfully.", "success")
            return redirect(url_for("manager_dashboard"))

    return render_template(
        "hostel_form.html",
        hostel=hostel,
        locations=HOSTEL_LOCATIONS,
        amenities=AMENITY_OPTIONS,
        photo_labels=PHOTO_LABELS,
    )


@app.route("/manager/hostels/<int:hostel_id>/photos", methods=["POST"])
@role_required("manager")
def hostel_photo_upload(hostel_id):
    hostel = db.session.get(Hostel, hostel_id) or abort(404)

    if not manager_owns_hostel(hostel):
        abort(403)

    label = request.form.get("label", "")
    file = request.files.get("photo")

    if label not in PHOTO_LABELS:
        flash("Please choose a photo label.", "danger")
    elif not file or file.filename == "":
        flash("Please choose an image file.", "danger")
    elif not allowed_file(file.filename):
        flash("Only JPG, PNG or WEBP images are allowed.", "danger")
    else:
        filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

        photo = HostelPhoto(hostel_id=hostel.id, label=label, filename=filename)
        db.session.add(photo)
        db.session.commit()
        flash("Photo uploaded and framed.", "success")

    return redirect(url_for("hostel_edit", hostel_id=hostel.id))


@app.route("/manager/photos/<int:photo_id>/delete", methods=["POST"])
@role_required("manager")
def hostel_photo_delete(photo_id):
    photo = db.session.get(HostelPhoto, photo_id) or abort(404)

    if not manager_owns_hostel(photo.hostel):
        abort(403)

    path = os.path.join(app.config["UPLOAD_FOLDER"], photo.filename)
    if os.path.exists(path):
        os.remove(path)

    hostel_id = photo.hostel_id
    db.session.delete(photo)
    db.session.commit()
    flash("Photo deleted.", "success")
    return redirect(url_for("hostel_edit", hostel_id=hostel_id))


@app.route("/manager/rooms/new", methods=["GET", "POST"])
@role_required("manager")
def room_new():
    my_hostels = Hostel.query.filter_by(manager_id=current_user.id).all()

    if request.method == "POST":
        hostel_id = request.form.get("hostel_id", type=int)
        room_type = request.form.get("room_type", "")
        quantity = request.form.get("quantity", type=int) or 1
        price = request.form.get("price", type=float) or 0

        hostel = db.session.get(Hostel, hostel_id) if hostel_id else None

        if not hostel or not manager_owns_hostel(hostel):
            flash("Please select one of your hostels.", "danger")
        elif room_type not in ROOM_TYPE_CAPACITY:
            flash("Please choose a valid room type.", "danger")
        elif quantity < 1:
            flash("Quantity must be at least 1.", "danger")
        elif price < 0:
            flash("Price cannot be negative.", "danger")
        else:
            room = Room(
                hostel_id=hostel.id,
                room_type=room_type,
                quantity=quantity,
                capacity=ROOM_TYPE_CAPACITY[room_type],
                price_per_year=Decimal(str(price)),
            )
            db.session.add(room)
            db.session.commit()
            flash(f"Room added: {quantity} x {room_type} = {quantity * ROOM_TYPE_CAPACITY[room_type]} slots.", "success")
            return redirect(url_for("manager_dashboard"))

    return render_template(
        "room_form.html",
        room=None,
        hostels=my_hostels,
        room_types=ROOM_TYPE_CAPACITY,
    )


@app.route("/manager/rooms/<int:room_id>/edit", methods=["GET", "POST"])
@role_required("manager")
def room_edit(room_id):
    room = db.session.get(Room, room_id) or abort(404)

    if not manager_owns_hostel(room.hostel):
        abort(403)

    my_hostels = Hostel.query.filter_by(manager_id=current_user.id).all()

    if request.method == "POST":
        room_type = request.form.get("room_type", "")
        quantity = request.form.get("quantity", type=int) or 1
        price = request.form.get("price", type=float) or 0

        if room_type not in ROOM_TYPE_CAPACITY:
            flash("Please choose a valid room type.", "danger")
        elif quantity < 1:
            flash("Quantity must be at least 1.", "danger")
        elif price < 0:
            flash("Price cannot be negative.", "danger")
        else:
            room.room_type = room_type
            room.quantity = quantity
            room.capacity = ROOM_TYPE_CAPACITY[room_type]
            room.price_per_year = Decimal(str(price))
            db.session.commit()
            flash("Room updated successfully.", "success")
            return redirect(url_for("manager_dashboard"))

    return render_template(
        "room_form.html",
        room=room,
        hostels=my_hostels,
        room_types=ROOM_TYPE_CAPACITY,
    )


@app.route("/manager/bookings/<int:booking_id>/checkin", methods=["POST"])
@role_required("manager", "admin")
def booking_checkin(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    if not manager_owns_hostel(booking.room.hostel):
        abort(403)

    if booking.status != "confirmed":
        flash("Only confirmed (paid) bookings can be checked in.", "warning")
    else:
        booking.status = "checked_in"
        notify(booking.student_id, f"You have been checked in at {booking.room.hostel.name}. Welcome!")
        db.session.commit()
        flash(f"{booking.student.username} checked in.", "success")

    return redirect(request.referrer or url_for("manager_dashboard"))


@app.route("/manager/bookings/<int:booking_id>/request-checkout", methods=["POST"])
@role_required("manager", "admin")
def booking_request_checkout(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    if not manager_owns_hostel(booking.room.hostel):
        abort(403)

    if booking.status != "checked_in":
        flash("Only checked-in students can be requested to check out.", "warning")
    else:
        booking.status = "checkout_requested"
        notify(booking.student_id, f"{booking.room.hostel.name} requested your check-out. Please approve or decline in My Bookings.")
        db.session.commit()
        flash("Check-out request sent. The student must approve it.", "info")

    return redirect(request.referrer or url_for("manager_dashboard"))


# -----------------------------
# Admin: oversight + applications + hostel removal + announcements
# -----------------------------

@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    stats = {
        "students": User.query.filter_by(role="student").count(),
        "managers": User.query.filter_by(role="manager").count(),
        "hostels": Hostel.query.count(),
        "rooms": Room.query.count(),
        "bookings": Booking.query.count(),
    }

    total_processed = db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).filter(Payment.status == "success").scalar()
    total_processed = Decimal(str(total_processed))

    payments_count = Payment.query.filter_by(status="success").count()
    service_earnings = money(payments_count * PLATFORM_FEE)
    gateway_collected = money(total_processed * PROCESSING_FEE_RATE)

    manager_rows = []
    for m in User.query.filter_by(role="manager").order_by(User.username).all():
        hostel_ids = [h.id for h in m.hostels]
        gross = Decimal("0")
        if hostel_ids:
            value = db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).select_from(Payment).join(Booking).join(Room).filter(Room.hostel_id.in_(hostel_ids), Payment.status == "success").scalar()
            gross = Decimal(str(value))
        manager_rows.append({
            "manager": m,
            "hostels": len(hostel_ids),
            "gross": gross,
        })

    hostel_rows = []
    for h in Hostel.query.order_by(Hostel.name).all():
        bookings_count = sum(len(r.bookings) for r in h.rooms)
        hostel_rows.append({"hostel": h, "bookings": bookings_count})

    announcements = Announcement.query.order_by(Announcement.created_at.desc()).all()
    recent_bookings = Booking.query.order_by(Booking.created_at.desc()).limit(10).all()

    return render_template(
        "admin_dashboard.html",
        stats=stats,
        total_processed=total_processed,
        service_earnings=service_earnings,
        gateway_collected=gateway_collected,
        manager_rows=manager_rows,
        hostel_rows=hostel_rows,
        announcements=announcements,
        recent_bookings=recent_bookings,
    )


@app.route("/admin/announcements", methods=["POST"])
@role_required("admin")
def admin_announcement_new():
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip()

    if not title or not body:
        flash("Announcement needs a title and a message.", "danger")
    else:
        db.session.add(Announcement(title=title, body=body))
        db.session.commit()
        flash("Announcement published on the homepage.", "success")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/announcements/<int:ann_id>/delete", methods=["POST"])
@role_required("admin")
def admin_announcement_delete(ann_id):
    announcement = db.session.get(Announcement, ann_id) or abort(404)
    db.session.delete(announcement)
    db.session.commit()
    flash("Announcement removed.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/hostels/<int:hostel_id>/delete", methods=["POST"])
@role_required("admin")
def admin_hostel_delete(hostel_id):
    hostel = db.session.get(Hostel, hostel_id) or abort(404)

    room_ids = [r.id for r in hostel.rooms]
    has_bookings = room_ids and Booking.query.filter(Booking.room_id.in_(room_ids)).count() > 0

    if has_bookings:
        flash("This hostel has bookings. Suspend the manager instead.", "warning")
    else:
        for photo in hostel.photos:
            path = os.path.join(app.config["UPLOAD_FOLDER"], photo.filename)
            if os.path.exists(path):
                os.remove(path)
        Review.query.filter_by(hostel_id=hostel.id).delete()
        for room in list(hostel.rooms):
            db.session.delete(room)
        db.session.delete(hostel)
        db.session.commit()
        flash("Hostel removed from the platform.", "success")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/applications")
@role_required("admin")
def admin_applications():
    applications = ManagerApplication.query.order_by(ManagerApplication.created_at.desc()).all()
    return render_template("admin_applications.html", applications=applications)


@app.route("/admin/applications/<int:app_id>/review", methods=["POST"])
@role_required("admin")
def admin_application_review(app_id):
    application = db.session.get(ManagerApplication, app_id) or abort(404)
    decision = request.form.get("decision", "")

    if application.status != "pending":
        flash("This application was already reviewed.", "warning")
    elif decision == "approve":
        if User.query.filter_by(phone=application.phone).first():
            flash("A user with this phone number already exists.", "danger")
        else:
            user = User(
                username=f"{application.first_name} {application.last_name}",
                first_name=application.first_name,
                last_name=application.last_name,
                phone=application.phone,
                email=f"{application.phone}@brightadel.com",
                role="manager",
            )
            user.password_hash = application.password_hash
            db.session.add(user)
            db.session.delete(application)
            db.session.flush()
            notify(user.id, "Congratulations! Your manager account is approved. You can now list your hostel.")
            db.session.commit()
            flash(f"{user.username} approved as a manager. They can now log in.", "success")
    elif decision == "reject":
        path = os.path.join(app.config["UPLOAD_FOLDER"], application.doc_filename)
        if os.path.exists(path):
            os.remove(path)
        db.session.delete(application)
        db.session.commit()
        flash("Application denied. All submitted information has been deleted.", "success")
    else:
        flash("Invalid decision.", "danger")

    return redirect(url_for("admin_applications"))


@app.route("/admin/users")
@role_required("admin")
def admin_users():
    users = User.query.order_by(User.created_at).all()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:user_id>/suspend", methods=["POST"])
@role_required("admin")
def admin_user_suspend(user_id):
    user = db.session.get(User, user_id) or abort(404)

    if user.id == current_user.id:
        flash("You cannot suspend your own account.", "warning")
    else:
        user.suspended = not user.suspended
        db.session.commit()
        action = "suspended" if user.suspended else "reactivated"
        flash(f"{user.username} has been {action}.", "success")

    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@role_required("admin")
def admin_user_delete(user_id):
    user = db.session.get(User, user_id) or abort(404)

    if user.id == current_user.id:
        flash("You cannot delete your own account.", "warning")
    elif user.role == "manager" and Hostel.query.filter_by(manager_id=user.id).count() > 0:
        flash("This manager owns hostels. Remove their hostels first.", "warning")
    elif Booking.query.filter_by(student_id=user.id).count() > 0:
        flash("This student has bookings. Remove their bookings first.", "warning")
    else:
        db.session.delete(user)
        db.session.commit()
        flash(f"{user.username} has been deleted.", "success")

    return redirect(url_for("admin_users"))


# -----------------------------
# Seed: backend admin ONLY
# -----------------------------

@app.cli.command("seed")
def seed_db():
    db.create_all()
    upgrade_database()

    if User.query.count() == 0:
        admin = User(
            username="Platform Admin",
            first_name="Platform",
            last_name="Admin",
            phone="0200000000",
            email="admin@brightadel.com",
            role="admin",
        )
        admin.set_password("admin123")
        db.session.add(admin)
        db.session.commit()

        print("Database ready.")
        print("Admin login -> phone: 0200000000 | password: admin123")


with app.app_context():
    db.create_all()
    upgrade_database()


if __name__ == "__main__":
    app.run(debug=True)