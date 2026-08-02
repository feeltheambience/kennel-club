#!/usr/bin/env bash
# Установка/обновление сервиса kennel-club на сервере (порт 8315)
set -e
cd /root/kennel-club
PORT=8315

# bind-тест: не занят ли порт чужим процессом
python3 - <<PY || { echo "ПОРТ $PORT ЗАНЯТ — правьте порт в install.sh и unit"; exit 1; }
import socket
s=socket.socket()
try:
    s.bind(("0.0.0.0",$PORT)); print("port $PORT free")
except OSError:
    raise SystemExit(1)
finally:
    s.close()
PY

python3 -m venv venv 2>/dev/null || true
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

cat > /etc/systemd/system/kennel-club.service <<'UNIT'
[Unit]
Description=Kennel club — учёт и выставки
After=network.target

[Service]
WorkingDirectory=/root/kennel-club
ExecStart=/root/kennel-club/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8315
Restart=always
MemoryMax=350M
Environment=KENNEL_PASSWORD=dogs

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable kennel-club >/dev/null 2>&1 || true
systemctl restart kennel-club

# ждём /health вместо мгновенного is-active (тот возвращает activating)
for i in $(seq 1 15); do
  if curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "OK: сервис отвечает на :$PORT"; exit 0
  fi
  sleep 1
done
echo "ВНИМАНИЕ: /health не ответил за 15с — смотрите journalctl -u kennel-club"
systemctl --no-pager status kennel-club | head -12
exit 1
