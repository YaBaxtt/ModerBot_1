# ModerBot

Production-oriented Telegram community bot: moderation, activity points, shop, reports, advertising applications, mandatory channel subscriptions, owner panel and broadcasts. The interface is Russian; commands remain Latin.

## Run locally

1. Create PostgreSQL database and copy `.env.example` to `.env`.
2. Fill `BOT_TOKEN`, `DATABASE_URL`, and at least one `OWNER_IDS` value.
3. Create tables with `alembic upgrade head`.
4. Install dependencies: `python -m pip install -r requirements.txt`.
5. Start: `python -m app.bot`.

For a temporary cloud deployment, use the included `Dockerfile` and follow `HOSTING.md`.

For moderation, make the bot an administrator in each group with **Ban users** and **Delete messages** permissions. Disable privacy mode in BotFather if command replies must be received in groups.

## Safety notes

- User identity is always Telegram ID; username is only a cached lookup convenience.
- Activity and shop debits use PostgreSQL atomic statements. Duplicate message updates and repeated purchase callbacks do not award/debit twice.
- `.env` is ignored; do not place tokens in code or migrations.
- The bot tracks only events it sees; it does not invent historical Telegram metrics.
