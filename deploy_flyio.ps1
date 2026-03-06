# ═══════════════════════════════════════════════════════════════════════════
# OpsHero Backend - Déploiement Fly.io (PowerShell)
# ═══════════════════════════════════════════════════════════════════════════

Write-Host "🚀 Déploiement OpsHero Backend sur Fly.io" -ForegroundColor Cyan
Write-Host ""

# Vérifier que Fly CLI est installé
if (-not (Get-Command fly -ErrorAction SilentlyContinue)) {
    Write-Host "❌ Fly CLI n'est pas installé" -ForegroundColor Red
    Write-Host "Installez avec: iwr https://fly.io/install.ps1 -useb | iex"
    exit 1
}

# Vérifier que .env existe
if (-not (Test-Path ".env")) {
    Write-Host "❌ Fichier .env manquant" -ForegroundColor Red
    exit 1
}

Write-Host "📋 Chargement des variables depuis .env..." -ForegroundColor Yellow
Write-Host ""

# Charger les variables du .env
$envVars = @{}
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^([^#][^=]+)=(.*)$') {
        $key = $matches[1].Trim()
        $value = $matches[2].Trim()
        $envVars[$key] = $value
    }
}

Write-Host "🔐 Configuration des secrets sur Fly.io..." -ForegroundColor Yellow
Write-Host ""

# Construire la commande fly secrets set
$secretsCmd = "fly secrets set "
$secretsCmd += "MONGODB_URL=`"$($envVars['MONGODB_URL'])`" "
$secretsCmd += "MONGODB_DB=`"$($envVars['MONGODB_DB'])`" "
$secretsCmd += "REDIS_URL=`"$($envVars['REDIS_URL'])`" "
$secretsCmd += "JWT_SECRET=`"$($envVars['JWT_SECRET'])`" "
$secretsCmd += "JWT_ALGORITHM=`"$($envVars['JWT_ALGORITHM'])`" "
$secretsCmd += "JWT_EXPIRE_HOURS=`"$($envVars['JWT_EXPIRE_HOURS'])`" "
$secretsCmd += "JWT_REFRESH_EXPIRE_DAYS=`"$($envVars['JWT_REFRESH_EXPIRE_DAYS'])`" "
$secretsCmd += "ADMIN_JWT_SECRET=`"$($envVars['ADMIN_JWT_SECRET'])`" "
$secretsCmd += "ADMIN_JWT_EXPIRE_HOURS=`"$($envVars['ADMIN_JWT_EXPIRE_HOURS'])`" "
$secretsCmd += "ADMIN_TOTP_ENCRYPTION_KEY=`"$($envVars['ADMIN_TOTP_ENCRYPTION_KEY'])`" "
$secretsCmd += "GITHUB_CLIENT_ID=`"$($envVars['GITHUB_CLIENT_ID'])`" "
$secretsCmd += "GITHUB_CLIENT_SECRET=`"$($envVars['GITHUB_CLIENT_SECRET'])`" "
$secretsCmd += "GROQ_API_KEY=`"$($envVars['GROQ_API_KEY'])`" "
$secretsCmd += "GROQ_BASE_URL=`"$($envVars['GROQ_BASE_URL'])`" "
$secretsCmd += "LLM_ENABLED=`"$($envVars['LLM_ENABLED'])`" "
$secretsCmd += "LLM_CONFIDENCE_THRESHOLD=`"$($envVars['LLM_CONFIDENCE_THRESHOLD'])`" "
$secretsCmd += "LLM_PRIMARY_MODEL=`"$($envVars['LLM_PRIMARY_MODEL'])`" "
$secretsCmd += "LLM_FAST_MODEL=`"$($envVars['LLM_FAST_MODEL'])`" "
$secretsCmd += "LLM_LONG_CONTEXT_MODEL=`"$($envVars['LLM_LONG_CONTEXT_MODEL'])`" "
$secretsCmd += "LLM_SHORT_LOG_THRESHOLD=`"$($envVars['LLM_SHORT_LOG_THRESHOLD'])`" "
$secretsCmd += "LLM_LONG_LOG_THRESHOLD=`"$($envVars['LLM_LONG_LOG_THRESHOLD'])`" "
$secretsCmd += "LLM_DAILY_BUDGET_USD=`"$($envVars['LLM_DAILY_BUDGET_USD'])`" "
$secretsCmd += "LLM_MONTHLY_BUDGET_USD=`"$($envVars['LLM_MONTHLY_BUDGET_USD'])`" "
$secretsCmd += "LLM_ALERT_THRESHOLD_PCT=`"$($envVars['LLM_ALERT_THRESHOLD_PCT'])`" "
$secretsCmd += "LLM_ENABLED_FOR_FREE=`"$($envVars['LLM_ENABLED_FOR_FREE'])`" "
$secretsCmd += "LLM_ENABLED_FOR_PRO=`"$($envVars['LLM_ENABLED_FOR_PRO'])`" "
$secretsCmd += "LLM_ENABLED_FOR_TEAM=`"$($envVars['LLM_ENABLED_FOR_TEAM'])`" "
$secretsCmd += "LLM_CALLS_PER_DAY_PRO=`"$($envVars['LLM_CALLS_PER_DAY_PRO'])`" "
$secretsCmd += "LLM_CALLS_PER_DAY_TEAM=`"$($envVars['LLM_CALLS_PER_DAY_TEAM'])`" "
$secretsCmd += "SMTP_HOST=`"$($envVars['SMTP_HOST'])`" "
$secretsCmd += "SMTP_PORT=`"$($envVars['SMTP_PORT'])`" "
$secretsCmd += "SMTP_USER=`"$($envVars['SMTP_USER'])`" "
$secretsCmd += "SMTP_PASSWORD=`"$($envVars['SMTP_PASSWORD'])`" "
$secretsCmd += "EMAIL_FROM=`"$($envVars['EMAIL_FROM'])`" "
$secretsCmd += "EMAIL_ENABLED=`"$($envVars['EMAIL_ENABLED'])`" "
$secretsCmd += "PATTERNS_DIR=`"$($envVars['PATTERNS_DIR'])`" "
$secretsCmd += "APP_ENV=`"production`" "
$secretsCmd += "DEBUG=`"false`""

# Exécuter la commande
Invoke-Expression $secretsCmd

Write-Host ""
Write-Host "✅ Secrets configurés!" -ForegroundColor Green
Write-Host ""
Write-Host "🚀 Déploiement en cours..." -ForegroundColor Cyan

fly deploy

Write-Host ""
Write-Host "✅ Déploiement terminé!" -ForegroundColor Green
Write-Host ""
Write-Host "📊 Commandes utiles:" -ForegroundColor Yellow
Write-Host "   fly logs          - Voir les logs"
Write-Host "   fly open          - Ouvrir l'app"
Write-Host "   fly status        - Vérifier le status"
Write-Host "   fly ssh console   - SSH dans le container"
