# Option A: wipe and reinitialize (destroys all DB + workspace data)
docker compose down -v
docker compose up -d --build

# Option B: keep existing data, just reset the password inside Postgres to match .env
docker compose exec postgres psql -U gateway_user -d gateway_db \
-c "ALTER USER gateway_user WITH PASSWORD '<the password currently in your .env>';"

docker compose exec postgres psql -U gateway_user -d gateway_db -c "ALTER USER gateway_user WITH PASSWORD 'm1ni0n123456';"
docker compose restart gateway