#! /bin/sh
# chmod +x update-proxy.sh
# sudo ln -s update-proxy.sh /usr/local/bin/update-proxy
cd /opt/cnit499-configs/app
docker compose down
git pull
docker compose up -d