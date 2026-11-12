# Adda's birthday treasure hunt

Flask + DeepSeek + IMAP IDLE (realtime) + SMTP, in Docker Compose.

## Set up (fish shell)

    cd treasure-hunt
    cp .env.example .env
    vi .env
    docker compose up -d --build
    docker compose logs -f

1. Create a mailbox on makketing.com (for example gamemaster@makketing.com) at any
   provider with IMAP and SMTP, set its MX, SPF and DKIM so mail to Gmail is not spam.
2. Fill in IMAP_*, SMTP_*, DEEPSEEK_API_KEY and ADMIN_TOKEN in .env.
3. Admin page is bound to 127.0.0.1:5137. Reach it with
   `ssh -L 5137:127.0.0.1:5137 yourserver` then open http://127.0.0.1:5137/admin?token=YOUR_TOKEN
   (no reverse proxy needed).

## Test on Friday

Mode starts as TEST: the player is TEST_PLAYER_EMAIL (you), subjects get "[TEST]", no time gates,
blackout, deadlines or nudges. Press "Send opening now" in the admin page, then reply from your
Gmail. The "Inject reply" box simulates replies without email.

## Saturday

In the admin page switch to LIVE (type LIVE). The opening goes out at 08:30 on 2026-11-14 to
LIVE_PLAYER_EMAIL, with gates, the 13:45-16:45 blackout, deadlines, nudges, the 19:30 bundle
and the 20:45 dinner reveal. You get a short status email at every step. Press "Reset game"
after testing, before LIVE.

## Review

All riddles, answers, gift reveals, gates and the 3-clue bank per riddle are in hunt.yaml.
After the 3 banked clues DeepSeek writes more (each easier; it never names the answer before
clue 6; shown in the admin page). Edit hunt.yaml, then `docker compose restart`.
Fill `answers` of the address stage with her old street name to make that check strict.

Run the tests: `python3 -m pytest -q tests`

## Deploy with GitHub Actions

Pushing to main runs the tests, rsyncs the project to `~/birthday-hunt` on the server (never
touching `.env`) and runs `docker compose up -d --build`.

One time, on the server (fish):

    mkdir -p ~/birthday-hunt
    cd ~/birthday-hunt
    vi .env              # copy from .env.example and fill in
    # public half of a dedicated deploy key goes in ~/.ssh/authorized_keys

Repo secrets (Settings, Secrets and variables, Actions): SERVER_HOST, SERVER_USER,
SERVER_SSH_KEY (private deploy key), SERVER_HOST_KEY (output of `ssh-keyscan -t ed25519 host`),
optional SERVER_PORT. The server needs rsync and docker access for the user.
