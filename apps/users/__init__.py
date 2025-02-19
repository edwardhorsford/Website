import time
from urllib.parse import urlparse, urljoin

from flask import (
    render_template,
    redirect,
    request,
    flash,
    url_for,
    abort,
    Blueprint,
    current_app as app,
    session,
    Markup,
    render_template_string,
)
from flask_login import login_user, login_required, logout_user, current_user
from flask_mailman import EmailMessage
from sqlalchemy import or_
from wtforms import StringField, SubmitField, BooleanField
from wtforms.validators import DataRequired, ValidationError

from main import db
from models.user import User, verify_signup_code
from models.cfp import Proposal, CFPMessage
from models.basket import Basket

from ..common import set_user_currency, feature_flag
from ..common.email import from_email
from ..common.forms import Form, EmailField


users = Blueprint("users", __name__)


@users.context_processor
def users_variables():
    unread_count = 0
    if current_user.is_authenticated:
        unread_count = (
            CFPMessage.query.join(Proposal)
            .filter(
                Proposal.user_id == current_user.id,
                Proposal.id == CFPMessage.proposal_id,
                CFPMessage.is_to_admin.is_(False),
                or_(
                    CFPMessage.has_been_read.is_(False),
                    CFPMessage.has_been_read.is_(None),
                ),
            )
            .count()
        )

    return {
        "unread_count": unread_count,
        "view_name": request.url_rule.endpoint.replace("users.", "."),
    }


def is_safe_url(target):
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ("http", "https") and ref_url.netloc == test_url.netloc


def get_next_url(default=None):
    next_url = request.args.get("next")
    if next_url:
        if is_safe_url(next_url):
            return next_url
        app.logger.error(f"Dropping unsafe next URL {repr(next_url)}")
    if default is None:
        default = url_for(".account")
    return default


class LoginForm(Form):
    email = EmailField("Email")

    def validate_email(form, field):
        user = User.get_by_email(form.email.data)
        if user is None:
            raise ValidationError("Email address not found")
        form._user = user


@users.route("/login/<email>")
@feature_flag("BYPASS_LOGIN")
def login_by_email(email):
    user = User.get_by_email(email)

    if current_user.is_authenticated:
        logout_user()

    if user is None:
        flash("Your email address was not recognised")
    else:
        login_user(user)
        session.permanent = True

    return redirect(get_next_url())


@users.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(get_next_url())

    if request.args.get("code"):
        user = User.get_by_code(app.config["SECRET_KEY"], request.args.get("code"))
        if user is not None:
            login_user(user)
            session.permanent = True
            return redirect(get_next_url())
        else:
            flash(
                "Your login link was invalid. Please enter your email address below to receive a new link."
            )

    form = LoginForm(request.form)
    if form.validate_on_submit():
        code = form._user.login_code(app.config["SECRET_KEY"])

        msg = EmailMessage(
            "Electromagnetic Field: Login details",
            from_email=from_email("TICKETS_EMAIL"),
            to=[form._user.email],
        )
        msg.body = render_template(
            "emails/login-code.txt",
            user=form._user,
            code=code,
            next_url=get_next_url(),
        )
        msg.send()

        flash("We've sent you an email with your login link")

    if request.args.get("email"):
        form.email.data = request.args.get("email")

    return render_template(
        "account/login.html",
        form=form,
        next=get_next_url(),
    )


@users.route("/logout")
@login_required
def logout():
    session.permanent = False
    Basket.clear_from_session()
    logout_user()
    return redirect(get_next_url(default=url_for("base.main")))


class SignupForm(Form):
    email = EmailField("Email")
    name = StringField("Name", [DataRequired()])
    allow_promo = BooleanField("Send me occasional emails about future EMF events")
    signup = SubmitField("Sign up")

    def validate_email(form, field):
        if User.does_user_exist(field.data):
            field.was_duplicate = True

            msg = Markup(
                render_template_string(
                    "Account already exists. "
                    'Please <a href="{{ url }}">click here</a> to log in.',
                    url=url_for("users.login", email=field.data),
                )
            )
            raise ValidationError(msg)


@users.route("/signup", methods=["GET", "POST"])
def signup():
    if not request.args.get("code"):
        abort(404)

    uid = verify_signup_code(
        app.config["SECRET_KEY"], time.time(), request.args.get("code")
    )
    if uid is None:
        flash(
            "Your signup link was invalid. Please note that they expire after 6 hours."
        )
        abort(404)

    user = User.query.get_or_404(uid)
    if not user.has_permission("admin"):
        app.logger.warn("Signup link resolves to non-admin user %s", user)
        abort(404)

    form = SignupForm()

    if current_user.is_authenticated:
        return redirect(url_for(".account"))

    if form.validate_on_submit():
        email, name = form.email.data, form.name.data
        user = User(email, name)

        if form.allow_promo.data:
            user.promo_opt_in = True

        db.session.add(user)
        db.session.commit()
        app.logger.info("Signed up new user with email %s and id %s", email, user.id)

        msg = EmailMessage(
            "Welcome to the EMF website",
            from_email=from_email("CONTACT_EMAIL"),
            to=[email],
        )
        msg.body = render_template("emails/signup-user.txt", user=user)
        msg.send()

        login_user(user)

        return redirect(url_for(".account"))

    if request.args.get("email"):
        form.email.data = request.args.get("email")

    return render_template("account/signup.html", form=form)


@users.route("/set-currency", methods=["POST"])
def set_currency():
    if request.form["currency"] not in ("GBP", "EUR"):
        abort(400)

    set_user_currency(request.form["currency"])
    db.session.commit()
    return redirect(url_for("tickets.main"))


@users.route("/sso/<site>")
def sso(site=None):

    volunteer_sites = [app.config["VOLUNTEER_SITE"]]
    if "VOLUNTEER_CAMP_SITE" in app.config:
        volunteer_sites.append(app.config["VOLUNTEER_CAMP_SITE"])

    if site not in volunteer_sites:
        abort(404)

    if not current_user.is_authenticated:
        return redirect(url_for(".login", next=url_for(".sso", site=site)))

    key = app.config["VOLUNTEER_SECRET_KEY"]
    sso_code = current_user.sso_code(key)

    return redirect("https://%s/?p=sso&c=%s" % (site, sso_code))


from . import account  # noqa
