# OpsHero Backend

FastAPI backend for OpsHero - CI/CD error analysis platform.

## Features

- 🚀 FastAPI async API
- 🧠 Hybrid analysis engine (regex + Groq LLM)
- 📊 MongoDB + Redis
- 🔐 GitHub OAuth + JWT
- 📧 Email notifications
- 🤖 Auto-learning system
- 👑 Admin panel API

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy environment file
cp .env.example .env
# Edit .env with your values

# Run
python start.py
```

## Environment Variables

See `.env.example` for all required variables.

Critical:
- `MONGODB_URL` - MongoDB connection string
- `REDIS_URL` - Redis connection string
- `GROQ_API_KEY` - Groq API key
- `GITHUB_CLIENT_ID` - GitHub OAuth client ID
- `GITHUB_CLIENT_SECRET` - GitHub OAuth secret
- `JWT_SECRET` - JWT signing secret

## Deploy on Fly.io

```bash
fly launch
fly secrets set MONGODB_URL="..." REDIS_URL="..." GROQ_API_KEY="..."
fly deploy
```

## API Documentation

Once running, visit:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## License

MIT
