"""@zakazKiT_bot — read-only Flowwow Seller order notifications.

All credentials must be passed as environment variables. Never commit them.
"""

import html
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests

FLOWWOW_BASE = 'https://apis.flowwow.com'
FLOWWOW_TOKEN = os.environ.get('FLOWWOW_TOKEN', '').strip()
TG_TOKEN = (os.environ.get('TELEGRAM_BOT_TOKEN') or os.environ.get('BOT_TOKEN') or '').strip()
SETUP_CODE = os.environ.get('SETUP_CODE', '').strip()
ENV_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
DATABASE_PATH = Path(os.environ.get('DATABASE_PATH', '/app/data/flowwow_orders.db' if Path('/app').exists() else './data/flowwow_orders.db'))
POLL_SECONDS = max(30, int(os.environ.get('POLL_SECONDS', '60')))
LOOKBACK_DAYS = max(1, min(7, int(os.environ.get('LOOKBACK_DAYS', '2'))))
MOSCOW = timezone(timedelta(hours=3))
API_TIMEOUT = 25
TG_ROOT = f'https://api.telegram.org/bot{TG_TOKEN}'

logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'),
                    format='%(asctime)s %(levelname)s %(message)s')
LOG = logging.getLogger('zakazKiT')
shutdown = threading.Event()


def db():
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    return conn


def init_db():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS config (
                     key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS shops_initialized (
                     shop_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS orders (
                     shop_id INTEGER NOT NULL, order_id INTEGER NOT NULL,
                     payload TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0,
                     PRIMARY KEY(shop_id, order_id))''')


def get_config(key):
    with db() as c:
        row = c.execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
        return row[0] if row else None


def put_config(key, value):
    with db() as c:
        c.execute('INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                  (key, str(value)))


def chat_id():
    """Private chat of the bot owner, established via /connect."""
    return int(ENV_CHAT_ID) if ENV_CHAT_ID else (int(get_config('chat_id')) if get_config('chat_id') else None)


def notification_chat_id():
    """Send new-order alerts to a bound group, or the owner's private chat."""
    group_id = get_config('notify_chat_id')
    return int(group_id) if group_id else chat_id()


def tg_call(method, payload, timeout=20):
    response = requests.post(f'{TG_ROOT}/{method}', json=payload, timeout=timeout)
    response.raise_for_status()
    content = response.json()
    if not content.get('ok'):
        raise RuntimeError(f'Telegram API returned ok=false in {method}')
    return content['result']


def tg_send(destination, body):
    tg_call('sendMessage', {'chat_id': destination, 'text': body[:4000], 'parse_mode': 'HTML',
                            'disable_web_page_preview': True}, timeout=15)


def flowwow(method, route, *, params=None, payload=None):
    response = requests.request(method, FLOWWOW_BASE + route,
        params=params, json=payload,
        headers={'Authorization': f'Bearer {FLOWWOW_TOKEN}', 'Accept': 'application/json'},
        timeout=API_TIMEOUT)
    if response.status_code == 429:
        try:
            delay = min(300, max(POLL_SECONDS, int(response.headers.get('Retry-After', POLL_SECONDS))))
        except ValueError:
            delay = POLL_SECONDS
        raise RateLimit(delay)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError(f'Unexpected Flowwow response shape: {route}')
    return body


class RateLimit(Exception):
    def __init__(self, delay):
        super().__init__(f'Rate limit, retry in {delay}s')
        self.delay = delay


def fetch_shops():
    all_shops = []
    page = 0  # Explicitly zero-based in the Flowwow /shops documentation.
    while True:
        response = flowwow('POST', '/apiseller/shops', payload={'status': 'active', 'page': page, 'limit': 50})
        entries = response.get('shops')
        if not isinstance(entries, list):
            raise ValueError('Flowwow /shops response does not include a shops list')
        all_shops.extend(entries)
        total = int(response.get('total', len(all_shops)))
        if len(all_shops) >= total or not entries:
            break
        page += 1
        if page > 100:
            raise ValueError('Too many pages in shops response')
    return {int(shop['shopId']): str(shop.get('name') or 'Кондитерская КиТ') for shop in all_shops}


def _order_ids(orders):
    return tuple(str(x.get('id')) for x in orders)


def fetch_orders(shop, created_date):
    params = {'shopId': shop, 'createdDate': created_date, 'limit': 100}
    data = flowwow('GET', '/apiseller/orders/list', params=params)
    first = data.get('items')
    if not isinstance(first, list):
        raise ValueError('Flowwow /orders/list response does not contain items list')
    total = int(data.get('total', len(first)))
    if total <= len(first):
        return first
    # The documentation doesn't explicitly state the page numbering for /orders/list.
    # Probe it only for shops with more than 100 orders created on one date.
    zero = flowwow('GET', '/apiseller/orders/list', params={**params, 'page': 0}).get('items')
    if isinstance(zero, list) and _order_ids(zero) == _order_ids(first):
        first_page = 0
    else:
        one = flowwow('GET', '/apiseller/orders/list', params={**params, 'page': 1}).get('items')
        if isinstance(one, list) and _order_ids(one) == _order_ids(first):
            first_page = 1
        else:
            raise ValueError('Cannot determine Flowwow order pagination numbering; aborting to avoid missing orders')
    result = list(first)
    for page in range(first_page + 1, first_page + 101):
        entries = flowwow('GET', '/apiseller/orders/list', params={**params, 'page': page}).get('items')
        if not isinstance(entries, list) or not entries:
            raise ValueError('Flowwow returned an incomplete orders page')
        result.extend(entries)
        if len(result) >= total:
            break
    if len(result) < total:
        raise ValueError(f'Flowwow order pages incomplete: {len(result)}/{total}')
    return result


def local_timestamp(ts):
    try:
        val = int(ts)
        if val > 10**11:
            val = val / 1000
        return datetime.fromtimestamp(val, MOSCOW).strftime('%d.%m.%Y %H:%M')
    except (TypeError, ValueError, OverflowError, OSError):
        return 'не указано'


def esc(value):
    return html.escape(str(value), quote=False)


def format_order(order, shop_name, *, title='🔔 <b>Новый заказ Flowwow!</b>'):
    order_id = order.get('id', '—')
    lines = [title, '',
             f'<b>Заказ № {esc(order_id)}</b>',
             f'🏪 Магазин: {esc(shop_name)}',
             f'🕒 Создан: {esc(local_timestamp(order.get("createdDate")))} (МСК)']
    if order.get('deliveryDateFrom'):
        delivery = local_timestamp(order.get('deliveryDateFrom'))
        if order.get('deliveryDateTo') and order['deliveryDateTo'] != order['deliveryDateFrom']:
            delivery += ' — ' + local_timestamp(order.get('deliveryDateTo'))
        lines.append(f'🚚 Доставка: {esc(delivery)} (МСК)')
    products = order.get('products') or []
    total = Decimal('0')
    currency = 'RUB'
    if products:
        lines += ['', '<b>Состав заказа:</b>']
        for p in products:
            if not isinstance(p, dict):
                continue
            try:
                total += Decimal(str(p.get('sum', 0)))
            except (InvalidOperation, ValueError):
                pass
            currency = str(p.get('currencyCode') or currency)
            lines.append(f'• {esc(p.get("name") or "Товар")} × {esc(p.get("count", 1))}')
            for prop in p.get('selectedProperties') or []:
                if isinstance(prop, dict) and prop.get('valueTitle'):
                    lines.append('    ' + esc(str(prop.get('propertyTitle') or 'Опция') + ': ' + str(prop['valueTitle'])))
        amount = f'{total:,.2f}'.replace(',', ' ').replace('.', ',')
        sign = {'RUB': '₽', 'EUR': '€', 'USD': '$'}.get(currency, currency)
        lines.append(f'💰 Сумма товаров: <b>{esc(amount)} {esc(sign)}</b>')
    for title, key in [('Адрес', 'address'), ('Комментарий', 'comment'),
                       ('Для магазина', 'shopAdditionalInfo'), ('Текст открытки', 'message')]:
        if order.get(key):
            lines.append(f'{esc(title)}: {esc(order[key])}')
    return '\n'.join(lines)[:3950]


def last_stored_order():
    """Return the newest order already synchronized from Flowwow.

    Compare creation timestamps rather than relying on order ID ordering.
    Does not request or modify any live Flowwow order.
    """
    with db() as c:
        rows = c.execute('SELECT shop_id, order_id, payload FROM orders').fetchall()
    latest = None
    latest_key = None
    for shop_id, order_id, payload in rows:
        try:
            order = json.loads(payload)
            if not isinstance(order, dict):
                continue
            raw_date = order.get('createdDate')
            timestamp = int(raw_date) if raw_date is not None else 0
            # Normalize milliseconds for comparisons.
            if timestamp > 10**11:
                timestamp //= 1000
            key = (timestamp, int(order_id))
        except (ValueError, TypeError, OverflowError, json.JSONDecodeError):
            continue
        if latest_key is None or key > latest_key:
            latest = (shop_id, order)
            latest_key = key
    return latest


def poll_once():
    destination = notification_chat_id()
    if destination is None:
        LOG.info('Waiting for Telegram connection: /connect <SETUP_CODE>')
        return
    shops = fetch_shops()
    if not shops:
        LOG.warning('No active Flowwow shops were found')
        return
    today = datetime.now(MOSCOW).date()
    for shop_id, name in shops.items():
        put_config(f'shop_name_{shop_id}', name)
        # Collect all requested dates first. A failed API call must not initialize a shop.
        fresh = {}
        for days_ago in range(LOOKBACK_DAYS):
            day = (today - timedelta(days=days_ago)).isoformat()
            for order in fetch_orders(shop_id, day):
                if not isinstance(order, dict) or order.get('id') is None:
                    raise ValueError('Flowwow returned an order without ID')
                fresh[int(order['id'])] = order
        with db() as c:
            existing = c.execute('SELECT 1 FROM shops_initialized WHERE shop_id=?', (shop_id,)).fetchone()
            baseline = not bool(existing)
            for item_id, payload in fresh.items():
                c.execute('''INSERT OR IGNORE INTO orders(shop_id,order_id,payload,notified)
                             VALUES(?,?,?,?)''',
                          (shop_id, item_id, json.dumps(payload, ensure_ascii=False), 1 if baseline else 0))
            if baseline:
                c.execute('INSERT INTO shops_initialized(shop_id) VALUES(?)', (shop_id,))
        if baseline:
            LOG.info('Shop %s initialized (%d existing orders ignored)', shop_id, len(fresh))
            tg_send(destination, f'✅ Мониторинг Flowwow включён. Магазин: <b>{esc(name)}</b>. '
                                 f'Ранее созданные заказы не отправляю.')
        with db() as c:
            pending = c.execute('''SELECT order_id,payload FROM orders
                                   WHERE shop_id=? AND notified=0 ORDER BY order_id''', (shop_id,)).fetchall()
        for item_id, payload in pending:
            try:
                tg_send(destination, format_order(json.loads(payload), name))
                with db() as c:
                    c.execute('UPDATE orders SET notified=1 WHERE shop_id=? AND order_id=?', (shop_id, item_id))
                LOG.info('Order %s/%s sent', shop_id, item_id)
            except Exception as exc:
                LOG.warning('Notification for order %s/%s failed: %s', shop_id, item_id, type(exc).__name__)
                break  # Keep all unsent orders for the next check.


def worker():
    while not shutdown.is_set():
        delay = POLL_SECONDS
        try:
            poll_once()
        except RateLimit as exc:
            delay = exc.delay
            LOG.warning('Flowwow rate limit; next check in %s seconds', delay)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else '?'
            LOG.error('Flowwow HTTP error: %s (check token/shop access if 401/403)', code)
        except Exception as exc:
            LOG.exception('Order check failed: %s', type(exc).__name__)
        shutdown.wait(delay)


def handle_update(update):
    msg = update.get('message') or {}
    where = msg.get('chat') or {}
    kind = where.get('type')
    if kind not in ('private', 'group', 'supergroup'):
        return
    ident = where.get('id')
    sender = (msg.get('from') or {}).get('id')
    command = (msg.get('text') or '').strip().split()
    if not command:
        return
    raw_action = command[0].lower()
    if not raw_action.startswith('/'):
        return
    name_and_target = raw_action.split('@', 1)
    if len(name_and_target) == 2 and name_and_target[1] != 'zakazkit_bot':
        return
    action = name_and_target[0]
    owner = chat_id()

    # Only private messages may contain the setup secret. Never share it in a group.
    if kind == 'private' and action == '/start':
        if owner == ident:
            tg_send(ident, '✅ @zakazKiT_bot подключён. Команды: /last, /status, /test, /group, /private, /help.')
        elif owner is not None:
            tg_send(ident, 'Этот бот уже привязан к владельцу.')
        else:
            tg_send(ident, 'Привет! Для подключения пришлите /connect КОД, установленный в Bothost.')
        return
    if kind == 'private' and action == '/connect' and owner is None:
        if not SETUP_CODE:
            tg_send(ident, 'SETUP_CODE ещё не задан в переменных окружения Bothost.')
        elif len(command) == 2 and len(SETUP_CODE) >= 8 and command[1] == SETUP_CODE:
            put_config('chat_id', ident)
            tg_send(ident, '✅ Telegram подключён. Проверка новых заказов Flowwow запущена.')
            LOG.info('Telegram owner chat connected')
        else:
            tg_send(ident, 'Неверный код. Проверьте SETUP_CODE в настройках Bothost.')
        return

    # The group is registered by the previously authenticated owner only.
    # Other members may see alerts but cannot change routing or request orders.
    authorized = (kind == 'private' and ident == owner or
                  kind in ('group', 'supergroup') and sender == owner and owner is not None)

    if kind in ('group', 'supergroup') and action == '/bindgroup':
        if not authorized:
            return
        old_group_id = get_config('notify_chat_id')
        put_config('notify_chat_id', ident)
        LOG.info('Notifications routed to a Telegram group')
        tg_send(ident, '✅ Группа подключена! Новые заказы Flowwow будут приходить сюда.\n'
                       'Управление доступно только владельцу бота.\n'
                       'Отключить: /unbindgroup@zakazKiT_bot')
        if old_group_id and str(old_group_id) != str(ident):
            LOG.info('Previous group was replaced')
        return
    if kind in ('group', 'supergroup') and action == '/unbindgroup':
        if not authorized or str(ident) != str(get_config('notify_chat_id')):
            return
        put_config('notify_chat_id', '')
        tg_send(ident, '✅ Группа отключена. Уведомления снова приходят владельцу в личные сообщения.')
        return
    if kind == 'private' and action == '/private' and authorized:
        put_config('notify_chat_id', '')
        tg_send(ident, '✅ Уведомления переключены в личный чат.')
        return
    if kind == 'private' and action == '/group' and authorized:
        group = get_config('notify_chat_id')
        if group:
            tg_send(ident, '✅ Уведомления направляются в подключённую группу.\n'
                           'Команда /private вернёт их в личный чат.')
        else:
            tg_send(ident, 'Сейчас уведомления приходят в личный чат.\n'
                           'Чтобы подключить группу, добавьте туда @zakazKiT_bot, '
                           'затем отправьте от своего аккаунта /bindgroup@zakazKiT_bot.')
        return

    if not authorized:
        return
    # Do not let commands in unrelated groups disclose any order data.
    if kind in ('group', 'supergroup') and str(ident) != str(get_config('notify_chat_id')):
        return
    if action == '/status':
        with db() as c:
            shops = c.execute('SELECT COUNT(*) FROM shops_initialized').fetchone()[0]
            total = c.execute('SELECT COUNT(*) FROM orders WHERE notified=1').fetchone()[0]
            queued = c.execute('SELECT COUNT(*) FROM orders WHERE notified=0').fetchone()[0]
        target = 'группа' if get_config('notify_chat_id') else 'личный чат'
        tg_send(ident, f'✅ Бот активен\nМагазинов: {shops}\nИзвестных заказов: {total}\nОжидают отправки: {queued}\nПроверка: каждые {POLL_SECONDS} сек.\nУведомления: {target}.')
    elif action == '/last':
        latest = last_stored_order()
        if latest is None:
            tg_send(ident, '📭 Пока нет сохранённых заказов Flowwow. Дождитесь первой успешной синхронизации.')
        else:
            shop_id, order = latest
            name = get_config(f'shop_name_{shop_id}') or f'Магазин №{shop_id}'
            tg_send(ident, format_order(order, name, title='📋 <b>Последний сохранённый заказ Flowwow</b>'))
    elif action == '/test':
        tg_send(ident, '🔔 Тестовое уведомление @zakazKiT_bot — Telegram подключён.')
    elif action == '/help':
        tg_send(ident, '/last — показать последний сохранённый заказ\n'
                       '/status — состояние\n/test — проверить доставку\n'
                       '/bindgroup — привязать группу (от владельца, в группе)\n'
                       '/unbindgroup — отвязать группу (от владельца, в группе)\n'
                       '/group — куда приходят уведомления (в личном чате)\n'
                       '/private — вернуть уведомления в личный чат\n/help — помощь')


def telegram_loop():
    offset = int(get_config('tg_offset') or 0)
    while not shutdown.is_set():
        try:
            updates = tg_call('getUpdates', {'offset': offset, 'timeout': 20, 'allowed_updates': ['message']}, timeout=30)
            for u in updates:
                current = int(u['update_id'])
                try:
                    handle_update(u)
                except Exception as exc:
                    LOG.warning('Telegram command handling error: %s', type(exc).__name__)
                offset = current + 1
                put_config('tg_offset', offset)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else '?'
            LOG.error('Telegram HTTP %s: verify BOT_TOKEN and disable conflicting webhook/other bot instances', status)
            shutdown.wait(15)
        except Exception as exc:
            LOG.warning('Telegram polling error: %s', type(exc).__name__)
            shutdown.wait(5)


def main():
    if not FLOWWOW_TOKEN:
        raise SystemExit('Missing FLOWWOW_TOKEN in environment')
    if not TG_TOKEN:
        raise SystemExit('Missing BOT_TOKEN / TELEGRAM_BOT_TOKEN in environment')
    if not ENV_CHAT_ID and not SETUP_CODE:
        raise SystemExit('Set SETUP_CODE (8+ chars) or TELEGRAM_CHAT_ID to restrict access')
    if SETUP_CODE and len(SETUP_CODE) < 8:
        raise SystemExit('SETUP_CODE must contain at least 8 characters')
    init_db()
    LOG.info('@zakazKiT_bot started; checking every %s seconds', POLL_SECONDS)
    task = threading.Thread(target=worker, daemon=True)
    task.start()
    try:
        telegram_loop()
    except KeyboardInterrupt:
        LOG.info('Stopping')
    finally:
        shutdown.set()
        task.join(timeout=5)


if __name__ == '__main__':
    main()
