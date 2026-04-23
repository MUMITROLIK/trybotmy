# Security Policy

## Sensitive Data Protection

This repository is configured to prevent accidental exposure of sensitive data:

### Protected Files (gitignored)

- `.env` - Contains API keys and tokens
- `signals.db` - Trading history database
- `*.log` - Log files
- `ml/model.joblib` - Large ML model files

### Safe to Commit

- `.env.example` - Template with placeholder values
- `docs/data.json` - Public statistics (no sensitive data)
- Source code files

## Setup Instructions

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```

2. Fill in your actual API keys in `.env`:
   - `TELEGRAM_BOT_TOKEN` - Get from @BotFather
   - `TELEGRAM_CHAT_ID` - Your Telegram chat ID
   - `CRYPTOPANIC_API_KEY` - From cryptopanic.com
   - `NETLIFY_TOKEN` - Optional, for deployment

3. Never commit `.env` file!

## Reporting Security Issues

If you discover a security vulnerability, please email [your-email] instead of opening a public issue.

## Best Practices

- Always use environment variables for secrets
- Never hardcode API keys in source code
- Rotate API keys regularly
- Use read-only API keys when possible
- Enable IP whitelisting on exchange APIs
