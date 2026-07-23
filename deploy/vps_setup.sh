#!/usr/bin/env bash
# One-time setup for the AI Radar nightly run on an Ubuntu Lightsail instance.
#
# Run this ON the VPS (paste into the Lightsail browser SSH terminal, or via
# a regular `ssh` session) AFTER the instance is created. It installs Python
# tooling, clones this public repo, and installs two cron jobs: a daily 7am
# pipeline run and a weekly Friday-5pm PDF digest (`main.py --digest`).
#
# It does NOT copy secrets (.env, radar.db, drive_token.json) - those never
# go through git. Copy them separately with scp from your machine, and edit
# config.yaml's llm.ollama section to point at Ollama Cloud (no local Ollama
# on this box) - see the chat instructions / README for both steps.

set -euo pipefail

REPO_URL="https://github.com/dvallslanaquera/ai-radar.git"
APP_DIR="$HOME/ai-radar"
TIMEZONE="Asia/Tokyo"     # change if you want cron's "7am" to mean something else

echo "==> Setting timezone to $TIMEZONE"
sudo timedatectl set-timezone "$TIMEZONE"

echo "==> Installing system packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git curl

echo "==> Installing Poetry"
curl -sSL https://install.python-poetry.org | python3 -
export PATH="$HOME/.local/bin:$PATH"
grep -q '.local/bin' ~/.bashrc || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

echo "==> Cloning ai-radar"
if [ -d "$APP_DIR" ]; then
  echo "    $APP_DIR already exists - pulling latest instead"
  git -C "$APP_DIR" pull
else
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
echo "==> poetry install"
poetry install

CRON_DAILY="0 7 * * * cd $APP_DIR && poetry run python main.py >> run.log 2>&1"
CRON_WEEKLY="0 17 * * 5 cd $APP_DIR && poetry run python main.py --digest >> digest.log 2>&1"
echo "==> Installing cron jobs ($TIMEZONE):"
echo "    daily  7am  pipeline : $CRON_DAILY"
echo "    weekly Fri 5pm digest : $CRON_WEEKLY"
# Replace any existing ai-radar entries with this pair (idempotent).
( crontab -l 2>/dev/null | grep -vF "$APP_DIR/main.py" ; echo "$CRON_DAILY" ; echo "$CRON_WEEKLY" ) | crontab -

cat <<'EOF'

==> Base setup done. Remaining manual steps before the first 7am run:
    1. scp these 3 files from your machine into ~/ai-radar/ on this VPS:
         .env  radar.db  drive_token.json
    2. Edit ~/ai-radar/config.yaml:
         llm.ollama.host  -> https://ollama.com
         llm.ollama.model -> gpt-oss:20b-cloud   (qwen3:8b is local-only)
    3. Test once by hand:
         cd ~/ai-radar && poetry run python main.py          # daily pipeline
         cd ~/ai-radar && poetry run python main.py --digest  # weekly PDF
EOF
