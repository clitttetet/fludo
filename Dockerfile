FROM python:3.11-slim

# Устанавливаем Tor и зависимости
RUN apt-get update && \
    apt-get install -y --no-install-recommends tor && \
    rm -rf /var/lib/apt/lists/*

# Пользователь с правами на /app
WORKDIR /app

# Ставим Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY bot.py .

# Готовим torrc под Railway (SOCKS5 на 127.0.0.1:8150)
RUN mkdir -p /var/lib/tor /var/log/tor && \
    printf 'SocksPort 127.0.0.1:8150\nControlPort 9051\nCookieAuthentication 0\nDataDirectory /var/lib/tor\nLog notice file /var/log/tor/notices.log\n' > /etc/tor/torrc

# Railway задаёт PORT, но для бота он не нужен
CMD ["python3", "-u", "bot.py"]
