"""Мониторинг доступности сайта septik-tmn.ru с уведомлениями на email.

Запускается по расписанию (GitHub Actions, каждые 5 минут). Состояние между
запусками хранится в state.json, поэтому письма приходят только при смене
состояния (упал / поднялся) и как напоминание, пока сайт лежит.

Только стандартная библиотека Python 3.9+.
"""
import argparse
import json
import os
import smtplib
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

# ---------------------------------------------------------------- настройки
DOMAIN = "septik-tmn.ru"
# Страницы для проверки и текст, который обязан быть на странице.
# Если хостинг отключит аккаунт, он отдаст свою заглушку с кодом 200 —
# по отсутствию маркера мы это поймаем.
PAGES = [
    ("https://septik-tmn.ru/", ["578-058", "Эко-Групп"]),
    ("https://septik-tmn.ru/catalog/", ["578-058"]),
]
# Признаки сломанного Bitrix/PHP при коде 200
ERROR_MARKERS = [
    "DB query error",
    "Ошибка подключения к базе",
    "Fatal error",
    "MySQL Query Error",
]
TIMEOUT = 30            # сек на один запрос
SLOW_SECONDS = 15       # медленнее — считаем проблемой
ATTEMPTS = 3            # попыток перед тем как признать сбой
RETRY_DELAY = 20        # сек между попытками
REMIND_HOURS = 3        # напоминать, пока сайт лежит
SSL_WARN_DAYS = 14      # предупредить об истечении SSL
DOMAIN_WARN_DAYS = 30   # предупредить об окончании оплаты домена

STATE_FILE = Path(__file__).with_name("state.json")
MSK = timezone(timedelta(hours=3))
UA = "Mozilla/5.0 (compatible; EcoGroupUptimeMonitor/1.0)"


# ---------------------------------------------------------------- проверки
def check_dns():
    try:
        ips = sorted({ai[4][0] for ai in socket.getaddrinfo(DOMAIN, 443, proto=socket.IPPROTO_TCP)})
        return ips, None
    except socket.gaierror as e:
        return [], f"DNS: домен {DOMAIN} не резолвится ({e}). Проблема с доменом или NS-серверами."


def check_tcp(ip):
    try:
        with socket.create_connection((ip, 443), timeout=TIMEOUT):
            return None
    except OSError as e:
        return f"Хостинг не отвечает: нет соединения с сервером {ip}:443 ({e})."


def check_page(url, markers):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        return f"{url} — сервер отвечает ошибкой HTTP {e.code} {e.reason}."
    except ssl.SSLError as e:
        return f"{url} — ошибка SSL-сертификата ({e})."
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return f"{url} — нет ответа ({reason})."
    elapsed = time.monotonic() - started

    if code != 200:
        return f"{url} — неожиданный код HTTP {code}."
    for m in ERROR_MARKERS:
        if m in body:
            return f"{url} — на странице ошибка сайта/базы данных: «{m}»."
    missing = [m for m in markers if m not in body]
    if missing:
        return (f"{url} — страница открывается, но это не наш сайт (нет текста "
                f"{', '.join('«%s»' % m for m in missing)}). Возможно, хостинг отключил "
                f"аккаунт и показывает заглушку.")
    if elapsed > SLOW_SECONDS:
        return f"{url} — страница грузится {elapsed:.0f} сек (порог {SLOW_SECONDS})."
    return None


def run_checks():
    """Возвращает список проблем (пустой — всё хорошо)."""
    ips, err = check_dns()
    if err:
        return [err]
    tcp_errors = [e for e in (check_tcp(ip) for ip in ips) if e]
    if len(tcp_errors) == len(ips):
        return tcp_errors
    return [e for e in (check_page(u, m) for u, m in PAGES) if e]


def check_with_retries():
    problems = []
    for attempt in range(1, ATTEMPTS + 1):
        problems = run_checks()
        if not problems:
            return []
        print(f"Попытка {attempt}/{ATTEMPTS}: " + " | ".join(problems))
        if attempt < ATTEMPTS:
            time.sleep(RETRY_DELAY)
    return problems


def ssl_days_left():
    ctx = ssl.create_default_context()
    with socket.create_connection((DOMAIN, 443), timeout=TIMEOUT) as sock:
        with ctx.wrap_socket(sock, server_hostname=DOMAIN) as s:
            not_after = s.getpeercert()["notAfter"]
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), timezone.utc)
    return (expires - datetime.now(timezone.utc)).days, expires


def domain_days_left():
    with socket.create_connection(("whois.tcinet.ru", 43), timeout=TIMEOUT) as s:
        s.sendall(f"{DOMAIN}\r\n".encode())
        data = b""
        while chunk := s.recv(4096):
            data += chunk
    for line in data.decode("utf-8", "replace").splitlines():
        if line.lower().startswith("paid-till:"):
            expires = datetime.fromisoformat(line.split(":", 1)[1].strip().replace("Z", "+00:00"))
            return (expires - datetime.now(timezone.utc)).days, expires
    raise ValueError("в ответе WHOIS нет paid-till")


# ---------------------------------------------------------------- почта
def send_email(subject, text, dry_run=False):
    text += ("\n\n—\nЭто автоматическое письмо от системы мониторинга сайта "
             f"{DOMAIN}. Отвечать на него не нужно.\n"
             "Журнал проверок: https://github.com/rifatism/septik-monitor/actions")
    if dry_run:
        print(f"\n[DRY RUN] письмо: {subject}\n{text}\n")
        return
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    to = [a.strip() for a in os.environ["MAIL_TO"].split(",") if a.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Мониторинг {DOMAIN} <{user}>"
    msg["To"] = ", ".join(to)
    msg["Auto-Submitted"] = "auto-generated"  # RFC 3834: автоответчики не будут отвечать
    msg.set_content(text)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=TIMEOUT) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    print(f"Письмо отправлено: {subject} -> {', '.join(to)}")


# ---------------------------------------------------------------- состояние
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"status": "up"}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fmt(dt):
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def fmt_duration(delta):
    minutes = int(delta.total_seconds() // 60)
    h, m = divmod(minutes, 60)
    d, h = divmod(h, 24)
    parts = [f"{d} д" if d else "", f"{h} ч" if h else "", f"{m} мин"]
    return " ".join(p for p in parts if p)


def daily_warnings(state, now, dry_run):
    """Раз в сутки проверяем сроки SSL и домена."""
    today = now.astimezone(MSK).date().isoformat()
    if state.get("expiry_checked") == today:
        return
    state["expiry_checked"] = today
    for name, func, limit in (
        ("SSL-сертификат", ssl_days_left, SSL_WARN_DAYS),
        ("Оплата домена", domain_days_left, DOMAIN_WARN_DAYS),
    ):
        try:
            days, expires = func()
        except Exception as e:  # сбой проверки срока не должен ронять мониторинг
            print(f"{name}: не удалось проверить ({e})")
            continue
        print(f"{name}: до {fmt(expires)}, осталось {days} дн.")
        if days <= limit:
            send_email(
                f"⚠️ {DOMAIN}: {name.lower()} истекает через {days} дн.",
                f"{name} для {DOMAIN} действует до {fmt(expires)} (осталось {days} дн.).\n\n"
                + ("Let's Encrypt обычно продлевается автоматически — если письмо пришло, "
                   "автопродление на хостинге (Beget → SSL) сломалось.\n"
                   if name.startswith("SSL") else
                   "Продлите домен у регистратора RU-CENTER (nic.ru), иначе сайт отключится.\n"),
                dry_run,
            )


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows (cp1251) не печатает эмодзи
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="печатать письма вместо отправки")
    parser.add_argument("--test-email", action="store_true", help="отправить тестовое письмо и выйти")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    if args.test_email:
        send_email(f"✅ Тест мониторинга {DOMAIN}",
                   f"Это тестовое письмо от мониторинга {DOMAIN} ({fmt(now)}).\n"
                   "Если вы его видите — уведомления о падении сайта будут приходить сюда.",
                   args.dry_run)
        return

    state = load_state()
    problems = check_with_retries()
    details = "\n".join(f"• {p}" for p in problems)

    if problems:
        print("САЙТ НЕДОСТУПЕН:\n" + details)
        if state["status"] == "up":
            state.update(status="down", since=now.isoformat(), last_alert=now.isoformat())
            send_email(f"🔴 {DOMAIN} НЕДОСТУПЕН",
                       f"Сайт {DOMAIN} перестал работать в {fmt(now)}.\n\n{details}\n\n"
                       f"Хостинг: Beget (cp.beget.com).\n"
                       f"Пока сайт лежит, напоминание будет приходить каждые {REMIND_HOURS} ч.",
                       args.dry_run)
        elif now - datetime.fromisoformat(state["last_alert"]) >= timedelta(hours=REMIND_HOURS):
            since = datetime.fromisoformat(state["since"])
            state["last_alert"] = now.isoformat()
            send_email(f"🔴 {DOMAIN} всё ещё недоступен ({fmt_duration(now - since)})",
                       f"Сайт {DOMAIN} не работает с {fmt(since)} — уже {fmt_duration(now - since)}.\n\n"
                       f"Сейчас:\n{details}",
                       args.dry_run)
        state["last_error"] = details
    else:
        print("Сайт работает.")
        if state["status"] == "down":
            since = datetime.fromisoformat(state["since"])
            send_email(f"✅ {DOMAIN} снова работает",
                       f"Сайт {DOMAIN} снова доступен с {fmt(now)}.\n"
                       f"Простой: {fmt_duration(now - since)} (с {fmt(since)}).\n\n"
                       f"Последняя ошибка:\n{state.get('last_error', '—')}",
                       args.dry_run)
            state = {k: v for k, v in state.items()
                     if k not in ("since", "last_alert", "last_error")}
            state["status"] = "up"
        daily_warnings(state, now, args.dry_run)

    # expiry_checked меняется раз в сутки -> ежедневный коммит state.json,
    # благодаря ему GitHub не отключает расписание за неактивность (60 дней)
    save_state(state)


if __name__ == "__main__":
    sys.exit(main())
