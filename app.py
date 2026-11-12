import hmac
import logging
import os

from flask import Flask, abort, make_response, redirect, render_template, request

from engine import Game, Hunt
from llm import DeepSeek
from mailer import SmtpMailer

logging.basicConfig(level=logging.INFO)

DATA = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA, exist_ok=True)
TOKEN = os.environ.get("ADMIN_TOKEN", "")

hunt = Hunt(os.environ.get("HUNT_FILE", "/app/hunt.yaml"))
game = Game(
    os.path.join(DATA, "hunt.db"), hunt, llm=DeepSeek(), mailer=SmtpMailer(),
    cfg={
        "mode": os.environ.get("HUNT_MODE", "TEST"),
        "player_test": os.environ["TEST_PLAYER_EMAIL"],
        "player_live": os.environ["LIVE_PLAYER_EMAIL"],
        "admin_email": os.environ.get("ADMIN_EMAIL", ""),
    },
)

if os.environ.get("START_WORKERS", "1") == "1":
    from worker import Workers
    Workers(game).start()

app = Flask(__name__)


def authed():
    if not TOKEN:
        abort(503, "ADMIN_TOKEN is not set")
    given = request.values.get("token") or request.cookies.get("token", "")
    if not hmac.compare_digest(given, TOKEN):
        abort(403)


@app.get("/health")
def health():
    return "ok"


@app.get("/admin")
def admin():
    authed()
    db = game.db
    ctx = dict(
        g=game, hunt=hunt, st=game.state, now=game.now(),
        log=db.execute("select ts,kind,text from log order by id desc limit 80").fetchall(),
        pending=db.execute("select send_after,to_addr,kind,substr(body,1,90) from outbox where sent_at is null order by id").fetchall(),
        sent=db.execute("select sent_at,to_addr,kind,body from outbox where sent_at is not null order by id desc limit 15").fetchall(),
    )
    resp = make_response(render_template("admin.html", **ctx))
    resp.set_cookie("token", TOKEN, httponly=True, samesite="Strict")
    return resp


@app.post("/admin/<action>")
def act(action):
    authed()
    f = request.form
    if action == "opening":
        game.send_opening()
    elif action == "mode":
        if f["mode"] == "LIVE" and f.get("confirm") != "LIVE":
            abort(400, "type LIVE to confirm")
        game.set_mode(f["mode"])
    elif action == "reset":
        if f.get("confirm") != "RESET":
            abort(400, "type RESET to confirm")
        game.reset()
    elif action == "pause":
        game.state["paused"] = not game.state["paused"]
        game.save()
    elif action == "jump":
        game.jump(f["stage"])
    elif action == "inject":
        game.handle_inbound(f["text"], None)
    elif action == "tick":
        pass
    game.tick()
    return redirect("/admin")
