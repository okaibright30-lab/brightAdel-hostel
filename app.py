import os
from datetime import datetime, date, timezone
from decimal import Decimal
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    abort,
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


basedir = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "brightadel-dev-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(basedir, "brightadel.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "warning"


ROOM_TYPES = ["single", "double", "twin", "dorm", "suite"]
ROOM_STATUSES = ["available", "occupied", "maintenance", "unavailable"]
BOOKING_STATUSES = ["pending", "confirmed", "checked_in", "checked_out", "cancelled"]


def utcnow():
    return datetime.now(timezone.utc)


# -----------------------------
# Models
# -----------------------------

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.username}>"


class Room(db.Model):
    __tablename__ = "rooms"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    room_type = db.Column(db.String(30), nullable=False, default="single")
    capacity = db.Column(db.Integer, nullable=False, default=1)
    price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    status = db.Column(db.String(30), nullable=False, default="available")
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    bookings = db.relationship("Booking", backref="room", lazy=True)

    def __repr__(self):
        return f"<Room {self.name}>"


class Guest(db.Model):
    __tablename__ = "guests"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    id_number = db.Column(db.String(80), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    bookings = db.relationship("Booking", backref="guest", lazy=True)

    def __repr__(self):
        return f"<Guest {self.full_name}>"


class Booking(db.Model):
    __tablename__ = "bookings"

    id = db.Column(db.Integer, primary_key=True)
    guest_id = db.Column(db.Integer, db.ForeignKey("guests.id"), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey("rooms.id"), nullable=False)
    check_in = db.Column(db.Date, nullable=False)
    check_out = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(30), nullable=False, default="pending")
    total_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f"<Booking {self.id}>"


# -----------------------------
# Flask Login Loader
# -----------------------------

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# -----------------------------
# Helpers
# -----------------------------

def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("login", next=request.url))

        if not current_user.is_admin:
            abort(403)

        return view(*args, **kwargs)

    return wrapped_view


def parse_date(value):
    if not value:
        return None

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def is_room_available(room_id, check_in, check_out, exclude_booking_id=None):
    active_statuses = ["pending", "confirmed", "checked_in"]

    query = Booking.query.filter(
        Booking.room_id == room_id,
        Booking.status.in_(active_statuses),
        Booking.check_in < check_out,
        Booking.check_out > check_in,
    )

    if exclude_booking_id:
        query = query.filter(Booking.id != exclude_booking_id)

    return query.count() == 0


# -----------------------------
# Public Pages
# -----------------------------

@app.route("/")
def index():
    rooms = Room.query.filter_by(status="available").order_by(Room.name).limit(6).all()
    return render_template("index.html", rooms=rooms)


# -----------------------------
# Authentication
# -----------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        error = None

        if not username or not email or not password:
            error = "All fields are required."
        elif password != confirm_password:
            error = "Passwords do not match."
        elif User.query.filter_by(username=username).first():
            error = "Username already exists."
        elif User.query.filter_by(email=email).first():
            error = "Email already exists."

        if error:
            flash(error, "danger")
        else:
            first_user = User.query.count() == 0

            user = User(
                username=username,
                email=email,
                is_admin=first_user,
            )
            user.set_password(password)

            db.session.add(user)
            db.session.commit()

            flash("Account created successfully. You can now log in.", "success")
            return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter(
            db.or_(
                User.username == identifier,
                User.email == identifier.lower(),
            )
        ).first()

        if user and user.check_password(password):
            login_user(user)
            flash(f"Welcome back, {user.username}!", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid username/email or password.", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


# -----------------------------
# Dashboard
# -----------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    total_rooms = Room.query.count()
    available_rooms = Room.query.filter_by(status="available").count()
    total_guests = Guest.query.count()

    active_bookings = Booking.query.filter(
        Booking.status.in_(["pending", "confirmed", "checked_in"])
    ).count()

    revenue = db.session.query(
        db.func.coalesce(db.func.sum(Booking.total_amount), 0)
    ).filter(
        Booking.status.in_(["confirmed", "checked_in", "checked_out"])
    ).scalar()

    recent_bookings = Booking.query.order_by(Booking.created_at.desc()).limit(8).all()

    return render_template(
        "dashboard.html",
        total_rooms=total_rooms,
        available_rooms=available_rooms,
        total_guests=total_guests,
        active_bookings=active_bookings,
        revenue=revenue,
        recent_bookings=recent_bookings,
    )


# -----------------------------
# Room Management
# -----------------------------

@app.route("/rooms")
@login_required
def rooms():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()

    query = Room.query

    if q:
        query = query.filter(
            db.or_(
                Room.name.ilike(f"%{q}%"),
                Room.room_type.ilike(f"%{q}%"),
            )
        )

    if status:
        query = query.filter(Room.status == status)

    room_list = query.order_by(Room.name).all()

    return render_template(
        "rooms.html",
        rooms=room_list,
        q=q,
        status=status,
        room_statuses=ROOM_STATUSES,
    )


@app.route("/rooms/new", methods=["GET", "POST"])
@admin_required
def room_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        room_type = request.form.get("room_type", "single")
        capacity = request.form.get("capacity", type=int) or 1
        price = request.form.get("price", type=float) or 0
        status = request.form.get("status", "available")
        notes = request.form.get("notes", "").strip()

        if not name:
            flash("Room name is required.", "danger")
        elif capacity < 1:
            flash("Capacity must be at least 1.", "danger")
        elif price < 0:
            flash("Price cannot be negative.", "danger")
        elif room_type not in ROOM_TYPES:
            flash("Invalid room type.", "danger")
        elif status not in ROOM_STATUSES:
            flash("Invalid room status.", "danger")
        else:
            room = Room(
                name=name,
                room_type=room_type,
                capacity=capacity,
                price=Decimal(str(price)),
                status=status,
                notes=notes,
            )

            db.session.add(room)
            db.session.commit()

            flash("Room created successfully.", "success")
            return redirect(url_for("rooms"))

    return render_template(
        "room_form.html",
        room=None,
        room_types=ROOM_TYPES,
        room_statuses=ROOM_STATUSES,
    )


@app.route("/rooms/<int:room_id>/edit", methods=["GET", "POST"])
@admin_required
def room_edit(room_id):
    room = db.session.get(Room, room_id) or abort(404)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        room_type = request.form.get("room_type", "single")
        capacity = request.form.get("capacity", type=int) or 1
        price = request.form.get("price", type=float) or 0
        status = request.form.get("status", "available")
        notes = request.form.get("notes", "").strip()

        if not name:
            flash("Room name is required.", "danger")
        elif capacity < 1:
            flash("Capacity must be at least 1.", "danger")
        elif price < 0:
            flash("Price cannot be negative.", "danger")
        elif room_type not in ROOM_TYPES:
            flash("Invalid room type.", "danger")
        elif status not in ROOM_STATUSES:
            flash("Invalid room status.", "danger")
        else:
            room.name = name
            room.room_type = room_type
            room.capacity = capacity
            room.price = Decimal(str(price))
            room.status = status
            room.notes = notes

            db.session.commit()

            flash("Room updated successfully.", "success")
            return redirect(url_for("rooms"))

    return render_template(
        "room_form.html",
        room=room,
        room_types=ROOM_TYPES,
        room_statuses=ROOM_STATUSES,
    )


@app.route("/rooms/<int:room_id>/delete", methods=["POST"])
@admin_required
def room_delete(room_id):
    room = db.session.get(Room, room_id) or abort(404)

    booking_count = Booking.query.filter_by(room_id=room.id).count()

    if booking_count > 0:
        flash(
            "This room has bookings. Mark it as unavailable instead of deleting it.",
            "warning",
        )
    else:
        db.session.delete(room)
        db.session.commit()
        flash("Room deleted successfully.", "success")

    return redirect(url_for("rooms"))


# -----------------------------
# Guest Management
# -----------------------------

@app.route("/guests")
@login_required
def guests():
    q = request.args.get("q", "").strip()

    query = Guest.query

    if q:
        query = query.filter(
            db.or_(
                Guest.full_name.ilike(f"%{q}%"),
                Guest.email.ilike(f"%{q}%"),
                Guest.phone.ilike(f"%{q}%"),
                Guest.id_number.ilike(f"%{q}%"),
            )
        )

    guest_list = query.order_by(Guest.full_name).all()

    return render_template(
        "guests.html",
        guests=guest_list,
        q=q,
    )


@app.route("/guests/new", methods=["GET", "POST"])
@admin_required
def guest_new():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        id_number = request.form.get("id_number", "").strip()
        notes = request.form.get("notes", "").strip()

        if not full_name:
            flash("Guest full name is required.", "danger")
        else:
            guest = Guest(
                full_name=full_name,
                email=email,
                phone=phone,
                id_number=id_number,
                notes=notes,
            )

            db.session.add(guest)
            db.session.commit()

            flash("Guest created successfully.", "success")
            return redirect(url_for("guests"))

    return render_template("guest_form.html", guest=None)


@app.route("/guests/<int:guest_id>/edit", methods=["GET", "POST"])
@admin_required
def guest_edit(guest_id):
    guest = db.session.get(Guest, guest_id) or abort(404)

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        id_number = request.form.get("id_number", "").strip()
        notes = request.form.get("notes", "").strip()

        if not full_name:
            flash("Guest full name is required.", "danger")
        else:
            guest.full_name = full_name
            guest.email = email
            guest.phone = phone
            guest.id_number = id_number
            guest.notes = notes

            db.session.commit()

            flash("Guest updated successfully.", "success")
            return redirect(url_for("guests"))

    return render_template("guest_form.html", guest=guest)


@app.route("/guests/<int:guest_id>/delete", methods=["POST"])
@admin_required
def guest_delete(guest_id):
    guest = db.session.get(Guest, guest_id) or abort(404)

    booking_count = Booking.query.filter_by(guest_id=guest.id).count()

    if booking_count > 0:
        flash("This guest has booking history and cannot be deleted.", "warning")
    else:
        db.session.delete(guest)
        db.session.commit()
        flash("Guest deleted successfully.", "success")

    return redirect(url_for("guests"))


# -----------------------------
# Booking Management
# -----------------------------

@app.route("/bookings")
@login_required
def bookings():
    status = request.args.get("status", "").strip()

    query = Booking.query

    if status:
        query = query.filter(Booking.status == status)

    booking_list = query.order_by(Booking.created_at.desc()).all()

    return render_template(
        "bookings.html",
        bookings=booking_list,
        status=status,
        booking_statuses=BOOKING_STATUSES,
    )


@app.route("/bookings/new", methods=["GET", "POST"])
@login_required
def booking_new():
    guests = Guest.query.order_by(Guest.full_name).all()
    rooms = Room.query.order_by(Room.name).all()

    if request.method == "POST":
        guest_id = request.form.get("guest_id", type=int)
        room_id = request.form.get("room_id", type=int)
        check_in = parse_date(request.form.get("check_in"))
        check_out = parse_date(request.form.get("check_out"))
        status = request.form.get("status", "pending")
        notes = request.form.get("notes", "").strip()

        guest = db.session.get(Guest, guest_id) if guest_id else None
        room = db.session.get(Room, room_id) if room_id else None

        if not guest or not room:
            flash("Please select both a guest and a room.", "danger")
        elif not check_in or not check_out:
            flash("Please provide valid check-in and check-out dates.", "danger")
        elif check_out <= check_in:
            flash("Check-out date must be after check-in date.", "danger")
        elif room.status == "unavailable":
            flash("This room is currently unavailable.", "danger")
        elif status not in BOOKING_STATUSES:
            flash("Invalid booking status.", "danger")
        elif not is_room_available(room.id, check_in, check_out):
            flash("This room is already booked for the selected dates.", "danger")
        else:
            nights = (check_out - check_in).days
            total_amount = Decimal(str(nights)) * room.price

            booking = Booking(
                guest_id=guest.id,
                room_id=room.id,
                check_in=check_in,
                check_out=check_out,
                status=status,
                total_amount=total_amount,
                notes=notes,
            )

            db.session.add(booking)
            db.session.commit()

            flash("Booking created successfully.", "success")
            return redirect(url_for("bookings"))

    return render_template(
        "booking_form.html",
        booking=None,
        guests=guests,
        rooms=rooms,
        booking_statuses=BOOKING_STATUSES,
    )


@app.route("/bookings/<int:booking_id>/edit", methods=["GET", "POST"])
@login_required
def booking_edit(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    guests = Guest.query.order_by(Guest.full_name).all()
    rooms = Room.query.order_by(Room.name).all()

    if request.method == "POST":
        guest_id = request.form.get("guest_id", type=int)
        room_id = request.form.get("room_id", type=int)
        check_in = parse_date(request.form.get("check_in"))
        check_out = parse_date(request.form.get("check_out"))
        status = request.form.get("status", "pending")
        notes = request.form.get("notes", "").strip()

        guest = db.session.get(Guest, guest_id) if guest_id else None
        room = db.session.get(Room, room_id) if room_id else None

        if not guest or not room:
            flash("Please select both a guest and a room.", "danger")
        elif not check_in or not check_out:
            flash("Please provide valid check-in and check-out dates.", "danger")
        elif check_out <= check_in:
            flash("Check-out date must be after check-in date.", "danger")
        elif room.status == "unavailable":
            flash("This room is currently unavailable.", "danger")
        elif status not in BOOKING_STATUSES:
            flash("Invalid booking status.", "danger")
        elif not is_room_available(room.id, check_in, check_out, exclude_booking_id=booking.id):
            flash("This room is already booked for the selected dates.", "danger")
        else:
            nights = (check_out - check_in).days
            total_amount = Decimal(str(nights)) * room.price

            booking.guest_id = guest.id
            booking.room_id = room.id
            booking.check_in = check_in
            booking.check_out = check_out
            booking.status = status
            booking.total_amount = total_amount
            booking.notes = notes

            db.session.commit()

            flash("Booking updated successfully.", "success")
            return redirect(url_for("bookings"))

    return render_template(
        "booking_form.html",
        booking=booking,
        guests=guests,
        rooms=rooms,
        booking_statuses=BOOKING_STATUSES,
    )


@app.route("/bookings/<int:booking_id>/delete", methods=["POST"])
@login_required
def booking_delete(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)

    db.session.delete(booking)
    db.session.commit()

    flash("Booking deleted successfully.", "success")
    return redirect(url_for("bookings"))


# -----------------------------
# Database Seeding
# -----------------------------

@app.cli.command("seed")
def seed_db():
    db.create_all()

    admin = User.query.filter_by(username="admin").first()

    if not admin:
        admin = User(
            username="admin",
            email="admin@brightadel.com",
            is_admin=True,
        )
        admin.set_password("admin123")
        db.session.add(admin)

    if Room.query.count() == 0:
        room_one = Room(
            name="Room 101",
            room_type="single",
            capacity=1,
            price=Decimal("25.00"),
            status="available",
            notes="Standard single room.",
        )

        room_two = Room(
            name="Room 102",
            room_type="double",
            capacity=2,
            price=Decimal("40.00"),
            status="available",
            notes="Comfortable double room.",
        )

        room_three = Room(
            name="Dorm A",
            room_type="dorm",
            capacity=6,
            price=Decimal("12.00"),
            status="available",
            notes="Shared dormitory.",
        )

        db.session.add_all([room_one, room_two, room_three])
        db.session.flush()

        guest = Guest(
            full_name="Amina Yusuf",
            email="amina@example.com",
            phone="+234 800 000 0001",
            id_number="ID-001",
            notes="First seeded guest.",
        )

        db.session.add(guest)
        db.session.flush()

        booking = Booking(
            guest_id=guest.id,
            room_id=room_one.id,
            check_in=date(2026, 8, 10),
            check_out=date(2026, 8, 12),
            status="confirmed",
            total_amount=Decimal("50.00"),
            notes="Sample booking.",
        )

        db.session.add(booking)

    db.session.commit()

    print("Database seeded successfully.")
    print("Admin login:")
    print("Username: admin")
    print("Password: admin123")


# -----------------------------
# Create Database Tables
# -----------------------------

with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(debug=True)