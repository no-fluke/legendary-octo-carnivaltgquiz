# 🤖 Telegram Quiz Scraper Bot

A Telegram bot that scrapes quiz polls from private channels using your Telegram account, auto-votes to reveal correct answers, and forwards them as native Telegram quiz polls to any destination chat.

---

## 📁 Project Structure

```
├── bot.py           # Main bot logic
├── db.py            # MongoDB helper functions
├── Dockerfile       # Docker container config
├── render.yaml      # Render.com deployment config
├── requirements.txt # Python dependencies
└── README.md        # This file
```

---

## ⚙️ Environment Variables

Set these in your Render dashboard (or `.env` for local development):

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Your Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_API_ID` | ✅ | Your Telegram API ID from [my.telegram.org](https://my.telegram.org/auth) |
| `TELEGRAM_API_HASH` | ✅ | Your Telegram API Hash from [my.telegram.org](https://my.telegram.org/auth) |
| `MONGO_URI` | ✅ | MongoDB connection string, e.g. `mongodb+srv://user:pass@cluster.mongodb.net/` |
| `MONGO_DB_NAME` | ❌ | MongoDB database name (default: `telegram_quiz_bot`) |

---

## 🚀 Setup & Deployment

### Step 1 — Create your Telegram Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the **BOT_TOKEN** it gives you

### Step 2 — Get Telegram API Credentials

1. Go to [https://my.telegram.org/auth](https://my.telegram.org/auth)
2. Log in with your phone number
3. Click **API development tools**
4. Create an app (any name and platform)
5. Copy your **API ID** and **API Hash**

### Step 3 — Create a MongoDB Atlas Database

1. Go to [https://mongodb.com/atlas](https://mongodb.com/atlas) and sign up free
2. Create a free **M0** cluster
3. Under **Database Access** → add a user with a username and password
4. Under **Network Access** → click **Add IP Address** → Allow Access from Anywhere (`0.0.0.0/0`)
5. Click **Connect** → **Drivers** → copy the connection string
6. Replace `<password>` in the string with your database user's password

### Step 4 — Deploy on Render

1. Push all files to a GitHub repository
2. Go to [https://render.com](https://render.com) and sign up
3. Click **New** → **Web Service**
4. Connect your GitHub repo — Render auto-detects the Dockerfile
5. Under **Environment**, add all 4 required environment variables (see table above)
6. Click **Deploy** — wait for the service to show **Live**

### Step 5 — Log in to the Bot

1. Open Telegram and message your bot
2. Send `/login`
3. Enter your phone number in international format (e.g. `+919876543210`)
4. Enter the OTP Telegram sends you
5. If you have 2FA enabled, enter your password too
6. Your session is saved to MongoDB — you only need to do this **once**

---

## 📖 Bot Commands

| Command | Description |
|---|---|
| `/start` | Show all available commands |
| `/login` | Log in with your Telegram account (required once) |
| `/status` | Check if you are currently logged in |
| `/addchannel` | Save a private channel to MongoDB |
| `/channels` | List all your saved channels |
| `/removechannel` | Remove a saved channel |
| `/set_destination` | Set a default destination for scraped quizzes |
| `/scrape` | Start scraping quizzes from a channel message range |
| `/cancel` | Cancel the current operation |

---

## 🔄 How Scraping Works

1. Send `/scrape` — pick a saved channel or paste a message link
2. Paste the **start** message link (first quiz in range)
3. Paste the **end** message link (last quiz in range)
4. Choose a destination (your chat, a channel, or a group)
5. The bot fetches all messages in that range, auto-votes on unattempted quizzes to reveal the correct answer, and forwards everything in the original channel order as native Telegram quiz polls

---

## 💾 What is stored in MongoDB

| Collection | Data |
|---|---|
| `users` | phone number, session string (Telethon auth), saved destination |
| `channels` | saved private channels per user (channel ID, title, link) |

> **Note:** The Telethon session string is stored securely in MongoDB so your login survives bot restarts and redeploys without needing a persistent disk.

---

## ⚠️ Notes

- The bot uses your **personal Telegram account** (via Telethon) to access private channels — you must be a member of the channel you want to scrape
- Auto-voting casts a random dummy vote to reveal the correct answer, then reads the result — this shows up in your Telegram vote history
- Render's free plan spins down after 15 minutes of inactivity. Use [UptimeRobot](https://uptimerobot.com) (free) to ping your service URL every 5 minutes to keep it alive
- Quiz images are downloaded temporarily to the container during scraping and sent via bot — they are not stored permanently

---

## 📦 Dependencies

```
python-telegram-bot>=20.0
telethon>=1.34
aiohttp>=3.8
motor>=3.3
pymongo>=4.6
```
