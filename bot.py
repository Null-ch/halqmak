import csv
import io
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv


TAG = "GO"
PAGE_SIZE = 8
LOG = logging.getLogger("halqmak")


class ApiError(Exception):
    def __init__(self, service, status=None):
        self.status = status
        super().__init__(f"{service}: {status or 'ошибка соединения или ответа'}")


class Api:
    def __init__(self, name, base, token=None):
        if urlsplit(base).scheme != "https" or not urlsplit(base).hostname:
            raise ValueError(f"{name}: требуется URL HTTPS")
        self.name = name
        self.base = base.rstrip("/")
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def call(self, method, path, *, timeout=15, **kwargs):
        try:
            response = self.session.request(
                method, self.base + path, timeout=timeout, allow_redirects=False, **kwargs
            )
            if not 200 <= response.status_code < 300:
                raise ApiError(self.name, response.status_code)
            return response.json()
        except (requests.RequestException, ValueError):
            raise ApiError(self.name) from None


class Panel:
    def __init__(self, api, expiry):
        self.api = api
        self.expiry = expiry

    def call(self, method, path, **kwargs):
        result = self.api.call(method, path, **kwargs)
        return result.get("response", result)

    def squad(self):
        squads = self.call("GET", "/internal-squads")["internalSquads"]
        matches = [squad["uuid"] for squad in squads if squad["name"] == TAG]
        if len(matches) != 1:
            raise ValueError("В панели должен быть ровно один внутренний сквад GO.")
        return matches[0]

    def create(self, username, squad):
        try:
            return self.call("POST", "/users", json={
                "username": username,
                "status": "ACTIVE",
                "tag": TAG,
                "expireAt": self.expiry,
                "trafficLimitBytes": 0,
                "trafficLimitStrategy": "NO_RESET",
                "activeInternalSquads": [squad],
            })
        except ApiError as error:
            if error.status is not None and error.status < 500 and error.status != 409:
                raise
            # A timeout can occur after the panel has committed the account.
            try:
                user = self.call("GET", f"/users/by-username/{username}")
            except ApiError:
                raise error from None
            if user.get("username") == username and user.get("tag") == TAG:
                return user
            raise error

    def active(self):
        start = 0
        now = datetime.now(timezone.utc)
        while True:
            page = self.call("GET", "/users", params={"start": start, "size": 250})
            users = page["users"]
            for user in users:
                if user.get("tag") != TAG or user.get("status") != "ACTIVE":
                    continue
                expires = datetime.fromisoformat(user["expireAt"].replace("Z", "+00:00"))
                if expires > now:
                    yield user
            start += len(users)
            if not users or start >= page["total"]:
                return


def button(text, action, style=None):
    result = {"text": text, "callback_data": action}
    if style:
        result["style"] = style
    return result


def subscription(user):
    url = user.get("subscriptionUrl") or ""
    return url if urlsplit(url).scheme in {"https", "http"} and urlsplit(url).hostname else ""


class Bot:
    def __init__(self, telegram, panel, admins, max_batch):
        self.telegram = telegram
        self.panel = panel
        self.admins = admins
        self.max_batch = max_batch
        self.confirmations = {}
        self.custom = set()
        self.views = {}

    def call(self, method, request_timeout=15, **payload):
        data = self.telegram.call("POST", "/" + method, json=payload, timeout=request_timeout)
        if not data["ok"]:
            raise ApiError("Telegram", data.get("error_code"))
        return data["result"]

    def screen(self, chat, text, rows=(), message=None):
        payload = {
            "chat_id": chat,
            "text": text,
            "reply_markup": {"inline_keyboard": list(rows)},
            "link_preview_options": {"is_disabled": True},
        }
        if message is not None:
            return self.call("editMessageText", message_id=message, **payload)
        return self.call("sendMessage", protect_content=True, **payload)

    def menu(self, chat, message=None):
        self.custom.discard(chat)
        self.confirmations.pop(chat, None)
        self.screen(chat, "Управление подписками GO", [
            [button("Создать одну", "count:1", "primary"),
             button("Создать 20", "count:20", "primary")],
            [button("Своё количество", "custom")],
            [button("Активные подписки GO", "active")],
        ], message)

    def confirm(self, chat, count, message=None):
        code = secrets.token_hex(8)
        self.confirmations[chat] = (code, count)
        self.screen(chat, f"Создать учётные записи: {count}\nТег и сквад: GO\nСрок в панели: до {self.panel.expiry[:10]}", [
            [button("Создать", f"create:{code}", "success"), button("Отмена", "menu")],
        ], message)

    def show(self, chat, code, index, message=None):
        current = self.views.get(chat)
        if current is None or code != current[0]:
            return self.screen(chat, "Список устарел. Откройте его заново.", [
                [button("Меню", "menu")],
            ], message)
        _, title, users = current
        pages = max(1, (len(users) + PAGE_SIZE - 1) // PAGE_SIZE)
        if not 0 <= index < pages:
            return
        rows = []
        for user in users[index * PAGE_SIZE:(index + 1) * PAGE_SIZE]:
            url = subscription(user)
            if not url:
                rows.append([button(user["username"] + " · ссылка отсутствует", "noop")])
                continue
            row = [{"text": user["username"], "url": url}]
            if len(url) <= 256:
                row.append({"text": "Копировать", "copy_text": {"text": url}})
            rows.append(row)
        navigation = []
        if index:
            navigation.append(button("←", f"page:{code}:{index - 1}"))
        if index + 1 < pages:
            navigation.append(button("→", f"page:{code}:{index + 1}"))
        if navigation:
            rows.append(navigation)
        if users:
            rows.append([button("Скачать CSV", f"csv:{code}"), button("Меню", "menu")])
        else:
            rows.append([button("Меню", "menu")])
        self.screen(chat, f"{title}\nСтраница {index + 1} из {pages}", rows, message)

    def results(self, chat, title, users, message=None):
        code = secrets.token_hex(6)
        self.views[chat] = (code, title, users)
        self.show(chat, code, 0, message)

    def export(self, chat, code):
        current = self.views.get(chat)
        if current is None or current[0] != code:
            return self.screen(chat, "Список устарел. Откройте его заново.")
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(["username", "subscription_url"])
        writer.writerows((user["username"], subscription(user)) for user in current[2])
        self.telegram.call("POST", "/sendDocument",
                           data={"chat_id": str(chat), "protect_content": "true"},
                           files={"document": ("GO.csv", buffer.getvalue().encode("utf-8-sig"), "text/csv")})

    def create(self, chat, code, message):
        pending = self.confirmations.get(chat)
        if pending is None or pending[0] != code:
            return self.screen(chat, "Подтверждение устарело.", [[button("Меню", "menu")]], message)
        del self.confirmations[chat]
        count = pending[1]
        squad = self.panel.squad()
        self.screen(chat, f"Создаю учётные записи: 0 из {count}", message=message)
        users, failures = [], []
        last_update = time.monotonic()
        for index in range(1, count + 1):
            username = f"go-{code}-{index:03d}"
            try:
                users.append(self.panel.create(username, squad))
            except ApiError as error:
                failures.append(username)
                LOG.warning("Creation failed for %s (%s)", username, error)
                if error.status in {401, 403, 429}:
                    break
            if index < count and time.monotonic() - last_update >= 3:
                last_update = time.monotonic()
                try:
                    self.screen(chat, f"Обработано: {index} из {count}", message=message)
                except ApiError:
                    pass
        title = f"Создано: {len(users)} из {count}"
        if failures:
            title += f"\nПанель не подтвердила: {len(failures)}"
        skipped = count - len(users) - len(failures)
        if skipped:
            title += f"\nПропущено: {skipped}"
        missing = sum(not subscription(user) for user in users)
        if missing:
            title += f"\nБез ссылки в ответе панели: {missing}"
        self.results(chat, title, users, message)

    def dispatch(self, update):
        callback = update.get("callback_query")
        message = (callback or update).get("message") or {}
        sender = (callback or message).get("from", {}).get("id")
        chat = message.get("chat", {}).get("id")
        allowed = sender in self.admins and chat == sender and message["chat"]["type"] == "private"
        if callback:
            answer = {"callback_query_id": callback["id"]}
            if not allowed:
                answer.update(text="Доступ закрыт.", show_alert=True)
            try:
                self.call("answerCallbackQuery", **answer)
            except ApiError as error:
                if error.status != 400:
                    raise
        if not allowed:
            return
        if not callback:
            value = message.get("text", "").strip()
            command = value.split(maxsplit=1)[0].split("@")[0] if value else ""
            if command in {"/start", "/menu", "/cancel"}:
                return self.menu(chat)
            if chat in self.custom:
                if value.isascii() and value.isdecimal() and len(value) <= 3:
                    count = int(value)
                    if 1 <= count <= self.max_batch:
                        self.custom.remove(chat)
                        return self.confirm(chat, count)
                return self.screen(chat, f"Введите целое число от 1 до {self.max_batch}.",
                                   [[button("Отмена", "menu")]])
            return
        action = callback.get("data", "")
        message_id = message["message_id"]
        if action == "menu":
            return self.menu(chat, message_id)
        if action == "custom":
            self.confirmations.pop(chat, None)
            self.custom.add(chat)
            return self.screen(chat, f"Сколько записей создать? Отправьте число от 1 до {self.max_batch}.",
                               [[button("Отмена", "menu")]], message_id)
        if action.startswith("count:"):
            self.custom.discard(chat)
            return self.confirm(chat, int(action.split(":")[1]), message_id)
        if action.startswith("create:"):
            return self.create(chat, action.split(":")[1], message_id)
        if action == "active":
            self.custom.discard(chat)
            self.confirmations.pop(chat, None)
            self.screen(chat, "Загружаю активные подписки GO…", message=message_id)
            users = list(self.panel.active())
            return self.results(chat, f"Активные подписки GO: {len(users)}", users, message_id)
        if action.startswith("page:"):
            _, code, index = action.split(":")
            return self.show(chat, code, int(index), message_id)
        if action.startswith("csv:"):
            return self.export(chat, action.split(":")[1])

    def run(self):
        offset = 0
        delay = 1
        while True:
            try:
                updates = self.call("getUpdates", request_timeout=40, timeout=30, offset=offset,
                                    allowed_updates=["message", "callback_query"])
                delay = 1
            except ApiError as error:
                LOG.warning("Polling failed (%s)", error)
                time.sleep(delay)
                delay = min(30, delay * 2)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.dispatch(update)
                except (ApiError, KeyError, ValueError, TypeError) as error:
                    LOG.warning("Update failed (%s)", error if isinstance(error, ApiError) else type(error).__name__)
                    envelope = update.get("callback_query") or update
                    chat = (envelope.get("message") or {}).get("chat", {}).get("id")
                    if chat in self.admins:
                        try:
                            self.screen(chat, "Не удалось завершить операцию. Проверьте активные подписки GO.",
                                        [[button("Активные подписки GO", "active"), button("Меню", "menu")]])
                        except ApiError:
                            pass


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    admins = {int(value.strip()) for value in os.environ["TELEGRAM_ADMIN_IDS"].split(",")}
    max_batch = int(os.getenv("MAX_BATCH", "100"))
    if min(admins) <= 0 or not 20 <= max_batch <= 999:
        raise ValueError("Проверьте TELEGRAM_ADMIN_IDS и MAX_BATCH (20–999).")
    telegram_url = os.environ["TELEGRAM_API_URL"].rstrip("/")
    telegram = Api("Telegram", telegram_url + "/bot" + os.environ["TELEGRAM_BOT_TOKEN"])
    expiry = os.getenv("REM_EXPIRE_AT", "2099-01-01T00:00:00.000Z")
    if datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
        raise ValueError("REM_EXPIRE_AT должна быть будущей датой с часовым поясом.")
    panel = Panel(Api("Remnawave", os.environ["REM_BASE_URL"], os.environ["REM_API_TOKEN"]), expiry)
    Bot(telegram, panel, admins, max_batch).run()


if __name__ == "__main__":
    main()
