#!/bin/bash

# ═══════════════════════════════════════════════════════════════════════════
# OpsHero Backend - Déploiement Fly.io
# ═══════════════════════════════════════════════════════════════════════════

set -e

echo "🚀 Déploiement OpsHero Backend sur Fly.io"
echo ""

# Vérifier que Fly CLI est installé
if ! command -v fly &> /dev/null; then
    echo "❌ Fly CLI n'est pas installé"
    echo "Installez avec: curl -L https://fly.io/install.sh | sh"
    exit 1
fi

# Vérifier que .env existe
if [ ! -f ".env" ]; then
    echo "❌ Fichier .env manquant"
    exit 1
fi

# Charger les variables du .env
source .env

echo "📋 Configuration des secrets depuis .env..."
echo ""

# Configurer tous les secrets
fly secrets set \
  MONGODB_URL="$MONGODB_URL" \
  MONGODB_DB="$MONGODB_DB" \
  REDIS_URL="$REDIS_URL" \
  JWT_SECRET="$JWT_SECRET" \
  JWT_ALGORITHM="$JWT_ALGORITHM" \
  JWT_EXPIRE_HOURS="$JWT_EXPIRE_HOURS" \
  JWT_REFRESH_EXPIRE_DAYS="$JWT_REFRESH_EXPIRE_DAYS" \
  ADMIN_JWT_SECRET="$ADMIN_JWT_SECRET" \
  ADMIN_JWT_EXPIRE_HOURS="$ADMIN_JWT_EXPIRE_HOURS" \
  ADMIN_TOTP_ENCRYPTION_KEY="$ADMIN_TOTP_ENCRYPTION_KEY" \
  GITHUB_CLIENT_ID="$GITHUB_CLIENT_ID" \
  GITHUB_CLIENT_SECRET="$GITHUB_CLIENT_SECRET" \
  GROQ_API_KEY="$GROQ_API_KEY" \
  GROQ_BASE_URL="$GROQ_BASE_URL" \
  LLM_ENABLED="$LLM_ENABLED" \
  LLM_CONFIDENCE_THRESHOLD="$LLM_CONFIDENCE_THRESHOLD" \
  LLM_PRIMARY_MODEL="$LLM_PRIMARY_MODEL" \
  LLM_FAST_MODEL="$LLM_FAST_MODEL" \
  LLM_LONG_CONTEXT_MODEL="$LLM_LONG_CONTEXT_MODEL" \
  LLM_SHORT_LOG_THRESHOLD="$LLM_SHORT_LOG_THRESHOLD" \
  LLM_LONG_LOG_THRESHOLD="$LLM_LONG_LOG_THRESHOLD" \
  LLM_DAILY_BUDGET_USD="$LLM_DAILY_BUDGET_USD" \
  LLM_MONTHLY_BUDGET_USD="$LLM_MONTHLY_BUDGET_USD" \
  LLM_ALERT_THRESHOLD_PCT="$LLM_ALERT_THRESHOLD_PCT" \
  LLM_ENABLED_FOR_FREE="$LLM_ENABLED_FOR_FREE" \
  LLM_ENABLED_FOR_PRO="$LLM_ENABLED_FOR_PRO" \
  LLM_ENABLED_FOR_TEAM="$LLM_ENABLED_FOR_TEAM" \
  LLM_CALLS_PER_DAY_PRO="$LLM_CALLS_PER_DAY_PRO" \
  LLM_CALLS_PER_DAY_TEAM="$LLM_CALLS_PER_DAY_TEAM" \
  SMTP_HOST="$SMTP_HOST" \
  SMTP_PORT="$SMTP_PORT" \
  SMTP_USER="$SMTP_USER" \
  SMTP_PASSWORD="$SMTP_PASSWORD" \
  EMAIL_FROM="$EMAIL_FROM" \
  EMAIL_ENABLED="$EMAIL_ENABLED" \
  PATTERNS_DIR="$PATTERNS_DIR" \
  APP_ENV="production" \
  DEBUG="false"

echo ""
echo "✅ Secrets configurés!"
echo ""
echo "🚀 Déploiement en cours..."
fly deploy

echo ""
echo "✅ Déploiement terminé!"
echo ""
echo "📊 Vérifier les logs:"
echo "   fly logs"
echo ""
echo "🌐 Ouvrir l'app:"
echo "   fly open"
echo ""
echo "📈 Status:"
echo "   fly status"
