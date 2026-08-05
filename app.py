import os
import random
import uuid
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
# CONFIGURATION
# -----------------------------
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")

# Paste an SMS provider key here later (mNotify / Hubtel / Twilio).
# While empty, verification codes are shown on screen (test mode).
SMS_API_KEY = os.environ.get("SMS_API_KEY", "")


HOSTEL_LOCATIONS = ["Ayensu", "Kwaprow", "Amamoma", "Old Site"]
AMENITY_OPTIONS = ["water", "electricity", "wifi"]
PHOTO_LABELS = ["Cover (Compound)", "Bathroom", "Bedroom", "Kitchen"]
ROOM_TYPE_CAPACITY = {
    "1 in a room": 1,
    "2 in a room": 2,
    "3 in a room": 3,
    "4 in a room": 4,
}
BOOKING_STATUSES = ["pending", "confirmed", "checked_in", "checked_out", "cancelled"]
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

COMMISSION_RATE = Decimal("0.02")


def utcnow():
    return datetime.now(timezone.utc)


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


def normalize_phone(value):
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if digits.startswith("233") and len(digits) == 12:
        digits = "0" + digits[3:]
    return digits


def valid_phone(digits):
    return digits.startswith("0") and len(digits) == 10


def send_sms(phone, message):
    if not SMS_API_KEY:
        print(f"[SMS TEST MODE] To {phone}: {message}")
        return True
    # Real SMS provider integration will go here later.
    return True


# -----------------------------
# Models
# -----------------------------

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), nullable=False)  # display full name
    first_name = db.Column(db.String(60), nullable=False)
    last_name = db.Column(db.String(60), nullable=False)
    phone = db.Column(db.String(15), unique=True, nullable=False)
    email = db.Column(db.String(120), nullable=False)  # synthetic, used by Paystack
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="student")
    suspended = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    hostels = db.relationship("Hostel", backref="manager", lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Hostel(db.Model):
    __tablename__ = "hostels"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(50), nullable=False)
    paid_amenities = db.Column(db.String(200), nullable=True, default="")
    has_ac = db.Column(db.Boolean, default=False, nullable=False)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    rooms = db.relationship("Room", backref="hostel", lazy=True)
    photos = db.relationship("HostelPhoto", backref="hostel", lazy=True)

    @property
    def paid_amenities_list(self):
        return [a for a in (self.paid_amenities or "").split(",") if a]


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
            Booking.status.in_(["confirmed", "checked_in"]),
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

    def commission(self):
        return money(self.paid_amount() * COMMISSION_RATE)


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    method = db.Column(db.String(50), nullable=False, default="paystack")
    status = db.Column(db.String(20), nullable=False, default="success")
    reference = db.Column(db.String(100), unique=True, nullable=False)
    paid_at = db.Column(db.DateTime, default=utcnow)


# -----------------------------
# Login loader + helpers
# -----------------------------

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


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


# -----------------------------
# Public pages + search system
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
        })

    if sort == "price_asc":
        cards.sort(key=lambda c: c["min_price"] if c["min_price"] is not None else Decimal("999999999"))
    elif sort == "price_desc":
        cards.sort(key=lambda c: c["min_price"] if c["min_price"] is not None else Decimal("0"), reverse=True)
    else:
        cards.sort(key=lambda c: c["hostel"].name)

    return render_template(
        "index.html",
        cards=cards,
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
    return render_template("hostel_detail.html", hostel=hostel, rooms=rooms, photos=photos)


# -----------------------------
# Authentication: register + SMS verify + login
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
        else:
            code = f"{random.randint(0, 999999):06d}"

            session["reg_data"] = {
                "first_name": first_name,
                "last_name": last_name,
                "phone": phone,
                "password_hash": generate_password_hash(password),
                "role": role,
            }
            session["reg_code"] = code
            session["reg_time"] = int(datetime.now().timestamp())

            send_sms(phone, f"BrightAdel verification code: {code}")

            flash("We sent a 6-digit verification code to your phone.", "info")
            return redirect(url_for("verify"))

    return render_template("register.html")


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
                role=data["role"],
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

    flash("A new code has been sent.", "info")
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
            flash("Invalid phone number or password.", "danger")

    return render_template("login.html")


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


# -----------------------------
# Student: bookings, payment, receipt, leave
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

        active = Booking.query.filter(
            Booking.student_id == current_user.id,
            Booking.status.in_(["pending", "confirmed", "checked_in"]),
        ).count()

        if active > 0:
            flash("You already have an active booking. Pay, cancel or check out of it first.", "danger")
        elif room.available_slots() <= 0:
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

        if PAYSTACK_SECRET_KEY:
            callback_url = url_for("payment_callback", booking_id=booking.id, _external=True)

            payload = {
                "email": current_user.email,
                "amount": int(amount * 100),
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
        amount = Decimal(str(data["data"]["amount"])) / Decimal("100")

        payment = Payment(
            booking_id=booking.id,
            amount=amount,
            method="paystack",
            status="success",
            reference=reference,
        )
        db.session.add(payment)

        if booking.status == "pending" and (booking.paid_amount() + amount) >= booking.half_fee():
            booking.status = "confirmed"

        db.session.commit()

        flash("Payment confirmed by Paystack. Your slot is secured!", "success")
        return redirect(url_for("booking_receipt", booking_id=booking.id))

    flash("Payment was not successful. Please try again.", "warning")
    return redirect(url_for("booking_pay", booking_id=booking.id))


@app.route("/bookings/<int:booking_id>/receipt")
@role_required("student", "admin")
def booking_receipt(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    if current_user.role == "student" and booking.student_id != current_user.id:
        abort(403)

    return render_template("receipt.html", booking=booking)


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


# -----------------------------
# Manager: hostels, rooms, photos, bookings
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

    hostel_ids = [h.id for h in hostels]
    gross_revenue = Decimal("0")
    if hostel_ids:
        value = db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).select_from(Payment).join(Booking).join(Room).filter(Room.hostel_id.in_(hostel_ids), Payment.status == "success").scalar()
        gross_revenue = Decimal(str(value))

    platform_fee = money(gross_revenue * COMMISSION_RATE)
    net_revenue = gross_revenue - platform_fee

    return render_template(
        "manager_dashboard.html",
        hostels=hostels,
        total_slots=total_slots,
        booked_slots=booked_slots,
        bookings=bookings,
        gross_revenue=gross_revenue,
        platform_fee=platform_fee,
        net_revenue=net_revenue,
    )


@app.route("/manager/hostels/new", methods=["GET", "POST"])
@role_required("manager")
def hostel_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        location = request.form.get("location", "")
        paid_amenities = request.form.getlist("paid_amenities")
        has_ac = request.form.get("has_ac") == "on"

        if not name:
            flash("Hostel name is required.", "danger")
        elif location not in HOSTEL_LOCATIONS:
            flash("Please choose a valid location.", "danger")
        else:
            hostel = Hostel(
                name=name,
                location=location,
                paid_amenities=",".join([a for a in paid_amenities if a in AMENITY_OPTIONS]),
                has_ac=has_ac,
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

        if not name:
            flash("Hostel name is required.", "danger")
        elif location not in HOSTEL_LOCATIONS:
            flash("Please choose a valid location.", "danger")
        else:
            hostel.name = name
            hostel.location = location
            hostel.paid_amenities = ",".join([a for a in paid_amenities if a in AMENITY_OPTIONS])
            hostel.has_ac = has_ac
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
        db.session.commit()
        flash(f"{booking.student.username} checked in.", "success")

    return redirect(request.referrer or url_for("manager_dashboard"))


@app.route("/manager/bookings/<int:booking_id>/checkout", methods=["POST"])
@role_required("manager", "admin")
def booking_checkout(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    if not manager_owns_hostel(booking.room.hostel):
        abort(403)

    if booking.status != "checked_in":
        flash("Only checked-in bookings can be checked out.", "warning")
    else:
        booking.status = "checked_out"
        db.session.commit()
        flash(f"{booking.student.username} checked out.", "success")

    return redirect(request.referrer or url_for("manager_dashboard"))


# -----------------------------
# Admin: oversight + 2% commission
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
    commission = money(total_processed * COMMISSION_RATE)
    managers_earned = total_processed - commission

    manager_rows = []
    for m in User.query.filter_by(role="manager").order_by(User.username).all():
        hostel_ids = [h.id for h in m.hostels]
        gross = Decimal("0")
        if hostel_ids:
            value = db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).select_from(Payment).join(Booking).join(Room).filter(Room.hostel_id.in_(hostel_ids), Payment.status == "success").scalar()
            gross = Decimal(str(value))
        fee = money(gross * COMMISSION_RATE)
        manager_rows.append({
            "manager": m,
            "hostels": len(hostel_ids),
            "gross": gross,
            "fee": fee,
            "net": gross - fee,
        })

    recent_bookings = Booking.query.order_by(Booking.created_at.desc()).limit(10).all()

    return render_template(
        "admin_dashboard.html",
        stats=stats,
        total_processed=total_processed,
        commission=commission,
        managers_earned=managers_earned,
        manager_rows=manager_rows,
        recent_bookings=recent_bookings,
    )


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
# Seed: backend admin ONLY (no demo data)
# -----------------------------

@app.cli.command("seed")
def seed_db():
    db.create_all()

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


if __name__ == "__main__":
    app.run(debug=True)