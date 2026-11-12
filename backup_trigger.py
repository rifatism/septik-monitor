"""Запускает ежедневную резервную копию на хостинге Beget.

Сам бэкап (дамп базы + архив файлов + загрузка на Google Drive через rclone)
выполняет скрипт ~/backup/backup.sh на сервере. Beget убивает процессы при
закрытии SSH-сессии, поэтому держим сессию открытой до конца работы скрипта.
При ошибке шлём письмо (как монитор).

Нужны секреты: BEGET_PASSWORD (+ SMTP_* и MAIL_TO для писем).
"""
import os
import sys

import paramiko

from monitor import send_email

HOST = "septiktmn.beget.tech"
USER = "septiktmn"
TIMEOUT_SEC = 120 * 60


def fail(text):
    print(text)
    send_email("❌ Резервная копия septik-tmn.ru НЕ сделана",
               text + "\n\nЛог на хостинге: ~/backup/backup.log")
    sys.exit(1)


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(HOST, username=USER, password=os.environ["BEGET_PASSWORD"],
                  timeout=30, look_for_keys=False, allow_agent=False)
    except Exception as e:
        fail(f"Не удалось подключиться к хостингу по SSH: {e}")
    c.get_transport().set_keepalive(30)
    print("Бэкап запущен на хостинге, жду результата...", flush=True)
    try:
        _, o, _ = c.exec_command(
            "bash ~/backup/backup.sh; echo EXIT=$?; "
            "tac ~/backup/backup.log | sed '/=== Старт/q' | tac",
            timeout=TIMEOUT_SEC)
        out = o.read().decode("utf-8", errors="replace")
    except Exception as e:
        fail(f"Связь с хостингом оборвалась или бэкап завис: {e}")
    finally:
        c.close()
    print(out)
    if "EXIT=0" not in out or "=== Готово" not in out:
        fail("Бэкап завершился с ошибкой:\n\n" + out)


if __name__ == "__main__":
    main()
