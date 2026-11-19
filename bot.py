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
STATUSES = {"ACTIVE": "Активна", "DISABLED": "Отключена", "LIMITED": "Лимит исчерпан", "EXPIRED": "Истекла"}
MENU_KEYS = (
    (("Создать одну", "count:1"), ("Создать 20", "count:20")),
    (("Своё количество", "custom"),),
    (("Активные GO", "active"), ("Все GO", "all")),
)
MENU_ACTIONS = {label: action for row in MENU_KEYS for label, action in row}
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
            if response.status_code == 204:
                return {}
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
            user = self.call("POST", "/users", json={
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
            if user.get("username") != username or user.get("tag") != TAG:
                raise error
        return self.rename_to_id(user)

    def rename_to_id(self, user):
        user_id = user.get("id")
        if type(user_id) is not int or user_id <= 0:
            LOG.warning("Panel did not return a numeric ID for %s", user["username"])
            return user
        username = f"go_{user_id}"
        try:
            updated = self.call("PATCH", "/users", json={"id": user_id, "username": username})
        except ApiError as error:
            LOG.warning("Could not confirm rename for ID %s (%s)", user_id, error)
            try:
                updated = self.call("GET", f"/users/{user_id}")
            except ApiError:
                return user
        if updated.get("id") == user_id:
            return {**user, **updated}
        return user

    def users(self, active_only=True):
        start = 0
        now = datetime.now(timezone.utc)
        while True:
            page = self.call("GET", "/users", params={"start": start, "size": 250})
            users = page["users"]
            for user in users:
                if user.get("tag") != TAG:
                    continue
                if active_only:
                    expires = datetime.fromisoformat(user["expireAt"].replace("Z", "+00:00"))
                    if user.get("status") != "ACTIVE" or expires <= now:
                        continue
                yield user
            start += len(users)
            if not users or start >= page["total"]:
                return

    def go_user(self, user_id):
        try:
            user = self.call("GET", f"/users/{user_id}")
        except ApiError as error:
            if error.status == 404:
                return None
            raise
        return user if user.get("id") == user_id and user.get("tag") == TAG else None

    def devices(self, user_id):
        try:
            result = self.call("GET", f"/hwid/devices/{user_id}")
        except ApiError as error:
            LOG.warning("HWID list unavailable (%s)", error)
            return None
        return result.get("devices")

    def external_squad(self, uuid):
        try:
            return self.call("GET", f"/external-squads/{uuid}").get("name") or uuid
        except ApiError:
            return uuid

    def change(self, user_id, action):
        user = self.go_user(user_id)
        if user is None:
            return None
        if action == "disable":
            if user.get("status") != "DISABLED":
                self.call("POST", f"/users/{user_id}/actions/disable")
        elif action == "delete":
            result = self.call("DELETE", f"/users/{user_id}")
            if result.get("isDeleted") is False:
                raise ApiError("Remnawave")
        else:
            raise ValueError("Unsupported account action")
        return user


def button(text, action, style=None):
    result = {"text": text, "callback_data": action}
    if style:
        result["style"] = style
    return result


def table_button(text, **action):
    return {"type": "button", "button": {"text": text, **action}}


def subscription(user):
    url = user.get("subscriptionUrl") or ""
    return url if urlsplit(url).scheme in {"https", "http"} and urlsplit(url).hostname else ""


def utc_date(value):
    if not value:
        return "—"
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return date.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def traffic(value):
    if value is None:
        return "—"
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if value < 1024 or unit == "ТиБ":
            return f"{value:.3g} {unit}"
        value /= 1024


class Bot:
    def __init__(self, telegram, panel, admins, max_batch):
        self.telegram = telegram
        self.panel = panel
        self.admins = admins
        self.max_batch = max_batch
        self.confirmations = {}
        self.inputs = {}
        self.actions = {}
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

    def table(self, chat, title, headers, data, rows, message=None, *, details=(), extra_tables=()):
        tables = (("", headers, data), *extra_tables)
        blocks = [{"type": "heading", "text": title, "size": 3}]
        blocks.extend({"type": "paragraph", "text": detail} for detail in details)
        for caption, columns, entries in tables:
            if caption:
                blocks.append({"type": "heading", "text": caption, "size": 4})
            cells = [[{"text": text, "is_header": True, "align": "left"} for text in columns]]
            cells.extend([{"text": value if isinstance(value, (dict, list)) else str(value),
                           "align": "left", "valign": "middle"}
                          for value in row] for row in entries)
            blocks.append({"type": "table", "cells": cells, "is_compact": True})
        content = {"blocks": blocks}
        payload = {"chat_id": chat, "rich_message": content, "reply_markup": {"inline_keyboard": rows}}
        try:
            if message is not None:
                return self.call("editMessageText", message_id=message, **payload)
            return self.call("sendRichMessage", protect_content=True, **payload)
        except ApiError as error:
            if error.status not in {400, 404}:
                raise
        lines, fallback_rows = [title, *details, ""], []
        for row in (row for _, _, entries in tables for row in entries):
            labels, buttons = [], []
            for value in row:
                for part in value if isinstance(value, list) else [value]:
                    if isinstance(part, dict):
                        fallback = {**part["button"]}
                        if fallback.get("style") == "link":
                            del fallback["style"]
                        buttons.append(fallback)
                    elif str(part).strip() and part != "—":
                        labels.append(str(part))
            if labels:
                lines.append(" · ".join(labels))
            if buttons:
                fallback_rows.append(buttons)
        return self.screen(chat, "\n".join(lines)[:3900], fallback_rows + rows, message)

    def menu(self, chat):
        self.inputs.pop(chat, None)
        self.actions.pop(chat, None)
        self.confirmations.pop(chat, None)
        return self.call("sendMessage", chat_id=chat, text="Управление подписками GO",
                         protect_content=True, reply_markup={
                             "keyboard": [[{"text": label} for label, _ in row] for row in MENU_KEYS],
                             "resize_keyboard": True,
                             "is_persistent": True,
                         })

    def confirm(self, chat, count, message=None):
        self.inputs.pop(chat, None)
        self.actions.pop(chat, None)
        code = secrets.token_hex(8)
        self.confirmations[chat] = (code, count)
        self.screen(chat, f"Создать учётные записи: {count}\nТег и сквад: GO\nСрок в панели: до {self.panel.expiry[:10]}", [
            [button("Создать", f"create:{code}", "success"), button("Отмена", "cancel")],
        ], message)

    def show(self, chat, code, index, message=None):
        current = self.views.get(chat)
        if current is None or code != current[0]:
            return self.screen(chat, "Список устарел. Откройте его заново.", message=message)
        _, title, users, details = current
        pages = max(1, (len(users) + PAGE_SIZE - 1) // PAGE_SIZE)
        if not 0 <= index < pages:
            return
        rows = []
        entries = users[index * PAGE_SIZE:(index + 1) * PAGE_SIZE]
        data = []
        for user in entries:
            url = subscription(user)
            user_id = user.get("id")
            name = user["username"]
            if user_id:
                name = table_button(name, callback_data=f"card:{user_id}:{index}", style="link")
            link = "—"
            if url and len(url) <= 256:
                link = table_button("Копировать", copy_text={"text": url})
            elif url:
                link = table_button("Открыть", url=url)
            data.append([name, link])
        navigation = []
        if index:
            navigation.append(button("←", f"page:{code}:{index - 1}"))
        if index + 1 < pages:
            navigation.append(button("→", f"page:{code}:{index + 1}"))
        if navigation:
            rows.append(navigation)
        if users:
            rows.append([button("Скачать CSV", f"csv:{code}")])
        if pages > 1:
            details = (*details, f"Страница {index + 1} из {pages}")
        if not entries:
            return self.screen(chat, "\n".join((title, *details)), rows, message)
        return self.table(chat, title, ("Подписка", "Ссылка"), data, rows, message, details=details)

    def results(self, chat, title, users, message=None, *, details=()):
        code = secrets.token_hex(6)
        self.views[chat] = (code, title, users, details)
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

    def back_to_list(self, chat, page):
        current = self.views.get(chat)
        if current:
            code, _, users, _ = current
            last = max(0, (len(users) - 1) // PAGE_SIZE)
            return button("← К списку", f"page:{code}:{min(page, last)}")
        return button("Все подписки GO", "all")

    def card(self, chat, user_id, page, message):
        self.inputs.pop(chat, None)
        self.confirmations.pop(chat, None)
        self.actions.pop(chat, None)
        user = self.panel.go_user(user_id)
        if user is None:
            return self.screen(chat, "Подписка GO недоступна.",
                               [[self.back_to_list(chat, page)]], message)
        usage = user.get("userTraffic") or user
        devices = self.panel.devices(user_id)
        squads = ", ".join(squad["name"] for squad in user.get("activeInternalSquads", []))
        limit = user.get("trafficLimitBytes")
        data = [
            ("ID", user_id),
            ("Статус", STATUSES.get(user.get("status"), "—")),
            ("Создана", utc_date(user.get("createdAt"))),
            ("Действует до", utc_date(user.get("expireAt"))),
            ("Первое подключение", utc_date(usage.get("firstConnectedAt"))),
            ("Последнее подключение", utc_date(usage.get("onlineAt"))),
            ("Трафик за период", f'{traffic(usage.get("usedTrafficBytes"))} / {traffic(limit) if limit != 0 else "без лимита"}'),
            ("Трафик за всё время", traffic(usage.get("lifetimeUsedTrafficBytes"))),
            ("Внутренние сквады", squads[:600] or "—"),
        ]
        if uuid := user.get("externalSquadUuid"):
            data.append(("Внешний сквад", self.panel.external_squad(uuid)))
        url = subscription(user)
        if url:
            data.append(("Подписка", table_button("Открыть", url=url)))
            if len(url) <= 256:
                data.append(("", table_button("Копировать", copy_text={"text": url})))
        extra_tables = []
        if devices:
            device_rows = [
                (
                    str(device.get("deviceModel") or "—")[:100],
                    "\n".join(str(device[field])[:100] for field in ("platform", "osVersion")
                              if device.get(field)) or "—",
                )
                for device in devices[:100]
            ]
            caption = f"Устройства HWID · {len(devices)}"
            if len(devices) > len(device_rows):
                caption += f" · показано {len(device_rows)}"
            extra_tables.append((caption, ("Модель", "Платформа / ОС"), device_rows))
        else:
            data.append(("Устройства HWID", "Нет устройств" if devices == [] else "Данные недоступны"))
        rows = []
        if user.get("status") != "DISABLED":
            rows.append([button("Деактивировать", f"action:disable:{user_id}:{page}")])
        rows.append([button("Удалить", f"action:delete:{user_id}:{page}", "danger")])
        rows.append([self.back_to_list(chat, page)])
        return self.table(chat, user["username"], ("Поле", "Значение"), data, rows, message,
                          extra_tables=extra_tables)

    def confirm_action(self, chat, action, user_id, page, message):
        self.inputs.pop(chat, None)
        self.confirmations.pop(chat, None)
        self.actions.pop(chat, None)
        user = self.panel.go_user(user_id)
        if user is None:
            return self.screen(chat, "Подписка GO недоступна.",
                               [[self.back_to_list(chat, page)]], message)
        token = secrets.token_hex(8)
        self.actions[chat] = (token, action, user_id, page, False)
        verb = "Удалить" if action == "delete" else "Деактивировать"
        return self.screen(chat, f"{verb} подписку {user['username']} (ID {user_id})?", [
            [button("Продолжить", f"verify:{token}"),
             button("Отмена", f"card:{user_id}:{page}")],
        ], message)

    def verify_action(self, chat, token, message):
        pending = self.actions.get(chat)
        if pending is None or pending[0] != token or pending[4]:
            return self.screen(chat, "Подтверждение устарело.", message=message)
        del self.actions[chat]
        _, action, user_id, page, _ = pending
        user = self.panel.go_user(user_id)
        if user is None:
            return self.screen(chat, "Подписка GO недоступна.",
                               [[self.back_to_list(chat, page)]], message)
        final_token = secrets.token_hex(8)
        self.actions[chat] = (final_token, action, user_id, page, True)
        verb = "удалить" if action == "delete" else "деактивировать"
        text = f"Вы точно хотите {verb} подписку {user['username']} (ID {user_id})?"
        if action == "delete":
            text += "\nУдаление нельзя отменить."
        return self.screen(chat, text, [
            [button(f"Да, {verb}", f"apply:{final_token}", "danger"),
             button("Отмена", f"card:{user_id}:{page}")],
        ], message)

    def apply_action(self, chat, token, message):
        pending = self.actions.get(chat)
        if pending is None or pending[0] != token or not pending[4]:
            return self.screen(chat, "Подтверждение устарело.", message=message)
        del self.actions[chat]
        _, action, user_id, page, _ = pending
        try:
            user = self.panel.change(user_id, action)
        except ApiError as error:
            LOG.warning("Account action %s failed for ID %s (%s)", action, user_id, error)
            return self.screen(chat, "Панель не подтвердила операцию. Обновите карточку.", [
                [button("Карточка", f"card:{user_id}:{page}")],
            ], message)
        # Other administrators may have the same subscription in their cached lists.
        self.views.clear()
        if user is None:
            text = "Подписка GO недоступна."
        elif action == "disable":
            text = f"Подписка {user['username']} деактивирована."
        else:
            text = f"Подписка {user['username']} удалена."
        return self.screen(chat, text, message=message)

    def create(self, chat, code, message):
        pending = self.confirmations.get(chat)
        if pending is None or pending[0] != code:
            return self.screen(chat, "Подтверждение устарело.", message=message)
        del self.confirmations[chat]
        _, count = pending
        squad = self.panel.squad()
        self.screen(chat, f"Создаю учётные записи: 0 из {count}", message=message)
        users, failures = [], []
        last_update = time.monotonic()
        for index in range(1, count + 1):
            username = f"go_{code}_{index:03d}"
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
        title = f"Создано подписок: {len(users)}"
        details = []
        if failures:
            details.append(f"Панель не подтвердила: {len(failures)}")
        skipped = count - len(users) - len(failures)
        if skipped:
            details.append(f"Пропущено: {skipped}")
        missing = sum(not subscription(user) for user in users)
        if missing:
            details.append(f"Без ссылки в ответе панели: {missing}")
        self.results(chat, title, users, message, details=details)

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
        if callback:
            action = callback.get("data", "")
            message_id = message["message_id"]
        else:
            value = message.get("text", "").strip()
            command = value.split(maxsplit=1)[0].split("@")[0] if value else ""
            if command in {"/start", "/menu", "/cancel"}:
                return self.menu(chat)
            action = MENU_ACTIONS.get(value)
            message_id = None
            if action is None and chat in self.inputs:
                if value.isascii() and value.isdecimal() and len(value) <= 3:
                    count = int(value)
                    if 1 <= count <= self.max_batch:
                        return self.confirm(chat, count)
                return self.screen(chat, f"Введите целое число от 1 до {self.max_batch}.",
                                   [[button("Отмена", "cancel")]])
            if action is None:
                return
        if action == "menu":
            return self.menu(chat)
        if action == "cancel":
            self.inputs.pop(chat, None)
            self.confirmations.pop(chat, None)
            return self.screen(chat, "Создание отменено.", message=message_id)
        if action == "custom":
            self.confirmations.pop(chat, None)
            self.actions.pop(chat, None)
            self.inputs[chat] = ("count",)
            return self.screen(chat, f"Сколько записей создать? Отправьте число от 1 до {self.max_batch}.",
                               [[button("Отмена", "cancel")]], message_id)
        if action.startswith("count:"):
            return self.confirm(chat, int(action.split(":")[1]), message_id)
        if action.startswith("create:"):
            return self.create(chat, action.split(":")[1], message_id)
        if action in {"active", "all"}:
            self.inputs.pop(chat, None)
            self.confirmations.pop(chat, None)
            self.actions.pop(chat, None)
            progress = self.screen(chat, "Загружаю подписки GO…", message=message_id)
            if message_id is None:
                message_id = progress["message_id"]
            users = list(self.panel.users(active_only=action == "active"))
            title = "Активные подписки GO" if action == "active" else "Все подписки GO"
            return self.results(chat, f"{title}: {len(users)}", users, message_id)
        if action.startswith("page:"):
            self.actions.pop(chat, None)
            _, code, index = action.split(":")
            return self.show(chat, code, int(index), message_id)
        if action.startswith("csv:"):
            return self.export(chat, action.split(":")[1])
        if action.startswith("card:"):
            _, user_id, page = action.split(":")
            return self.card(chat, int(user_id), int(page), message_id)
        if action.startswith("action:"):
            _, operation, user_id, page = action.split(":")
            if operation in {"disable", "delete"}:
                return self.confirm_action(chat, operation, int(user_id), int(page), message_id)
        if action.startswith("verify:"):
            return self.verify_action(chat, action.split(":")[1], message_id)
        if action.startswith("apply:"):
            return self.apply_action(chat, action.split(":")[1], message_id)

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
                                        [[button("Активные подписки GO", "active")]])
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
