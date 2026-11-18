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
POLL_SECONDS = max(30, int(os.environ.get('POLL_SECONDS', '30')))
MAX_OLDER_CHECKS = max(1, min(30, int(os.environ.get('MAX_OLDER_CHECKS', '10'))))
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
        columns = {row[1] for row in c.execute('PRAGMA table_info(orders)').fetchall()}
        # Safe, non-destructive migration for existing SQLite databases on Bothost.
        for column, definition in (
            ('status_code', 'INTEGER'),
            ('message_id', 'INTEGER'),
            ('message_chat_id', 'INTEGER'),
        ):
            if column not in columns:
                c.execute(f'ALTER TABLE orders ADD COLUMN {column} {definition}')
        c.execute('''CREATE TABLE IF NOT EXISTS status_events (
                     event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                     shop_id INTEGER NOT NULL,
                     order_id INTEGER NOT NULL,
                     old_status INTEGER,
                     new_status INTEGER NOT NULL,
                     delivered INTEGER NOT NULL DEFAULT 0,
                     created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')


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


def tg_send(destination, body, *, reply_markup=None):
    payload = {'chat_id': destination, 'text': body[:4000],
               'parse_mode': 'HTML', 'disable_web_page_preview': True}
    if reply_markup is not None:
        payload['reply_markup'] = reply_markup
    return tg_call('sendMessage', payload, timeout=15)


def tg_edit(destination, message_id, body, *, reply_markup=None):
    try:
        payload = {'chat_id': destination, 'message_id': message_id,
                   'text': body[:4000], 'parse_mode': 'HTML',
                   'disable_web_page_preview': True}
        if reply_markup is not None:
            payload['reply_markup'] = reply_markup
        return tg_call('editMessageText', payload, timeout=15)
    except requests.HTTPError as exc:
        message = exc.response.text.lower() if exc.response is not None else ''
        if 'message is not modified' in message:
            return None
        raise


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


STATUS_LABELS = {
    1: '🆕 Новый',
    2: '✅ Принят',
    3: '🏁 Завершён',
    4: '🚚 Передан в доставку',
    5: '📷 Фото до доставки',
    7: '🏪 Ожидает самовывоза',
    10: '📦 Заказ собран (почта)',
    11: '🚚 Доставляется почтой',
    12: '📮 Ожидает получения на почте',
}


def status_value(order):
    raw = order.get('status') if isinstance(order, dict) else None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def status_label(code):
    return STATUS_LABELS.get(code, f'Статус №{code}' if code is not None else 'Неизвестен')


def format_order(order, shop_name, *, title='🔔 <b>Новый заказ Flowwow!</b>'):
    order_id = order.get('id', '—')
    lines = [title, '',
             f'<b>Заказ № {esc(order_id)}</b>',
             f'🏪 Магазин: {esc(shop_name)}',
             f'🕒 Создан: {esc(local_timestamp(order.get("createdDate")))} (МСК)']
    if order.get('status') is not None:
        lines.append(f'📌 Статус: <b>{esc(status_label(status_value(order)))}</b>')
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


def fetch_order_detail(shop_id, order_id):
    """Fetch an older active order even when it is outside LOOKBACK_DAYS."""
    item = flowwow('GET', '/apiseller/orders/view',
                   params={'shopId': shop_id, 'orderId': order_id})
    if int(item.get('id', -1)) != int(order_id):
        raise ValueError('Flowwow returned mismatched order ID for /orders/view')
    return item


def refresh_stored_orders(shop_id, fresh):
    """Check older unfinished orders in bounded rotating batches."""
    with db() as c:
        rows = c.execute('''SELECT order_id,payload,status_code FROM orders
                            WHERE shop_id=? AND (status_code IS NULL OR status_code != 3)
                            ORDER BY order_id''', (shop_id,)).fetchall()
    remaining = []
    for oid, payload, status in rows:
        if oid in fresh:
            continue
        if status is None:
            try:
                if status_value(json.loads(payload)) == 3:
                    continue
            except (ValueError, TypeError):
                pass
        remaining.append(oid)
    if not remaining:
        return
    cursor_key = f'older_cursor_{shop_id}'
    start = int(get_config(cursor_key) or 0) % len(remaining)
    chosen = [remaining[(start + idx) % len(remaining)]
              for idx in range(min(len(remaining), MAX_OLDER_CHECKS))]
    put_config(cursor_key, start + len(chosen))
    for order_id in chosen:
        try:
            fresh[order_id] = fetch_order_detail(shop_id, order_id)
        except RateLimit:
            raise
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else '?'
            LOG.warning('Cannot refresh older order %s/%s (HTTP %s)', shop_id, order_id, status)
        except Exception as exc:
            LOG.warning('Cannot refresh older order %s/%s: %s', shop_id, order_id, type(exc).__name__)


def sync_shop_orders(shop_id, fresh):
    """Persist latest API state and enqueue each detected status transition once."""
    with db() as c:
        baseline = c.execute('SELECT 1 FROM shops_initialized WHERE shop_id=?', (shop_id,)).fetchone() is None
        for order_id, payload in fresh.items():
            status = status_value(payload)
            raw = json.dumps(payload, ensure_ascii=False)
            prior = c.execute('''SELECT payload,notified,status_code
                                 FROM orders WHERE shop_id=? AND order_id=?''',
                              (shop_id, order_id)).fetchone()
            if prior is None:
                c.execute('''INSERT INTO orders(shop_id,order_id,payload,notified,status_code)
                             VALUES(?,?,?,?,?)''',
                          (shop_id, order_id, raw, 1 if baseline else 0, status))
                continue
            old_payload, notified, stored_status = prior
            if stored_status is None:
                try:
                    stored_status = status_value(json.loads(old_payload))
                except (ValueError, TypeError):
                    stored_status = None
            if status is not None and stored_status is not None and status != stored_status and notified:
                c.execute('''INSERT INTO status_events(shop_id,order_id,old_status,new_status)
                             VALUES(?,?,?,?)''', (shop_id, order_id, stored_status, status))
                LOG.info('Status changed for order %s/%s: %s -> %s',
                         shop_id, order_id, stored_status, status)
            # Keep payload synchronized, even when status is unchanged.
            c.execute('''UPDATE orders SET payload=?,status_code=? WHERE shop_id=? AND order_id=?''',
                      (raw, status if status is not None else stored_status, shop_id, order_id))
        if baseline:
            c.execute('INSERT INTO shops_initialized(shop_id) VALUES(?)', (shop_id,))
    return baseline


def is_missing_edit_target(exc):
    if not isinstance(exc, requests.HTTPError) or exc.response is None:
        return False
    if exc.response.status_code not in (400, 404):
        return False
    body = exc.response.text.lower()
    return ('message to edit not found' in body or
            "message can't be edited" in body or
            'message_id_invalid' in body)


def deliver_shop_updates(shop_id, name, destination):
    with db() as c:
        pending = c.execute('''SELECT o.order_id,o.payload,o.notified,o.message_id,o.message_chat_id
             FROM orders o
             WHERE o.shop_id=? AND (o.notified=0 OR EXISTS (
                 SELECT 1 FROM status_events e
                 WHERE e.shop_id=o.shop_id AND e.order_id=o.order_id AND e.delivered=0))
             ORDER BY o.order_id''', (shop_id,)).fetchall()
    for oid, raw, notified, message_id, message_chat in pending:
        try:
            current = json.loads(raw)
            card = format_order(current, name, title='🍰 <b>Заказ Flowwow</b>')
            if message_id is not None and message_chat == destination:
                try:
                    tg_edit(destination, message_id, card)
                except requests.HTTPError as exc:
                    if not is_missing_edit_target(exc):
                        raise
                    result = tg_send(destination, card)
                    message_id = int(result['message_id'])
            else:
                result = tg_send(destination, card)
                message_id = int(result['message_id'])
            with db() as c:
                c.execute('''UPDATE orders SET notified=1,message_id=?,message_chat_id=?
                             WHERE shop_id=? AND order_id=?''',
                          (message_id, destination, shop_id, oid))
            with db() as c:
                events = c.execute('''SELECT event_id,old_status,new_status
                    FROM status_events WHERE shop_id=? AND order_id=? AND delivered=0
                    ORDER BY event_id''', (shop_id, oid)).fetchall()
            for event_id, old_status, new_status in events:
                note = (f'🔄 <b>Статус заказа №{esc(oid)} изменился</b>\n'
                        f'🏪 {esc(name)}\n'
                        f'{esc(status_label(old_status))} → <b>{esc(status_label(new_status))}</b>')
                tg_send(destination, note)
                with db() as c:
                    c.execute('UPDATE status_events SET delivered=1 WHERE event_id=?', (event_id,))
            LOG.info('Order %s/%s card synchronized; %s status alerts', shop_id, oid, len(events))
        except Exception as exc:
            LOG.warning('Notification for order %s/%s failed: %s',
                        shop_id, oid, type(exc).__name__)
            break


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
        fresh = {}
        for days_ago in range(LOOKBACK_DAYS):
            day = (today - timedelta(days=days_ago)).isoformat()
            for order in fetch_orders(shop_id, day):
                if not isinstance(order, dict) or order.get('id') is None:
                    raise ValueError('Flowwow returned an order without ID')
                fresh[int(order['id'])] = order
        refresh_stored_orders(shop_id, fresh)
        baseline = sync_shop_orders(shop_id, fresh)
        if baseline:
            LOG.info('Shop %s initialized (%d existing orders ignored)', shop_id, len(fresh))
            tg_send(destination, f'✅ Мониторинг Flowwow включён. Магазин: <b>{esc(name)}</b>. '
                                 f'Ранее созданные заказы не отправляю.')
        deliver_shop_updates(shop_id, name, destination)
    put_config('last_sync_at', datetime.now(MOSCOW).strftime('%d.%m.%Y %H:%M:%S'))


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



# Inline keyboard keeps working in groups with Telegram's Privacy Mode enabled.
# A command in Telegram's native Menu button opens this keyboard.
MENU_CATEGORIES = {
    'n': ('🆕 Новые', (1,)),
    'a': ('✅ Принятые', (2, 5)),
    'd': ('🚚 На доставке', (4, 11)),
    'p': ('🏪 Самовывоз', (7, 12)),
    'c': ('🏁 Завершённые', (3,)),
    'o': ('📦 Другие', (10,)),
}
MENU_PAGE_SIZE = 6


def menu_button(label, data):
    return {'text': label, 'callback_data': data}


def stored_orders():
    """Read the locally synchronized orders. Browsing the menu does not call Flowwow."""
    with db() as c:
        rows = c.execute('SELECT shop_id,order_id,payload,status_code FROM orders').fetchall()
    result = []
    for shop_id, oid, raw, code in rows:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                continue
            actual = code if code is not None else status_value(payload)
            timestamp = payload.get('createdDate')
            timestamp = int(timestamp) if timestamp is not None else 0
            if timestamp > 10**11:
                timestamp //= 1000
            result.append((shop_id, oid, payload, actual, timestamp))
        except (ValueError, TypeError, OverflowError, json.JSONDecodeError):
            LOG.warning('Skipping malformed locally stored order %s/%s', shop_id, oid)
    return sorted(result, key=lambda item: (item[4], item[1]), reverse=True)


def category_for_status(code):
    for key, (_, statuses) in MENU_CATEGORIES.items():
        if code in statuses:
            return key
    return 'o'


def menu_counts(rows):
    counts = {key: 0 for key in MENU_CATEGORIES}
    for _, _, _, code, _ in rows:
        counts[category_for_status(code)] += 1
    return counts


def menu_home():
    rows = stored_orders()
    counts = menu_counts(rows)
    last_sync = get_config('last_sync_at') or 'ещё нет'
    body = ('🍰 <b>Кондитерская КиТ · Flowwow</b>\n'
            'Выбери раздел, чтобы посмотреть заказы.\n'
            f'Всего в базе: <b>{len(rows)}</b>\n'
            f'Последняя синхронизация: {esc(last_sync)} (МСК)\n'
            'Данные из базы, опрос API каждые 30 сек.')
    markup = {'inline_keyboard': [
        [menu_button(f'🆕 Новые · {counts["n"]}', 'km:n:0'),
         menu_button(f'✅ Принятые · {counts["a"]}', 'km:a:0')],
        [menu_button(f'🚚 На доставке · {counts["d"]}', 'km:d:0'),
         menu_button(f'🏪 Самовывоз · {counts["p"]}', 'km:p:0')],
        [menu_button(f'🏁 Завершённые · {counts["c"]}', 'km:c:0'),
         menu_button(f'📦 Другие · {counts["o"]}', 'km:o:0')],
        [menu_button('🕒 Последний заказ', 'km:l'),
         menu_button('📊 Статистика', 'km:s')],
        [menu_button('🔄 Обновить список', 'km:h')]
    ]}
    return body, markup


def menu_section(category, requested_page=0):
    if category not in MENU_CATEGORIES:
        return menu_home()
    title, _ = MENU_CATEGORIES[category]
    all_items = [row for row in stored_orders()
                 if category_for_status(row[3]) == category]
    pages = max(1, (len(all_items) + MENU_PAGE_SIZE - 1) // MENU_PAGE_SIZE)
    page = min(max(0, requested_page), pages - 1)
    items = all_items[page * MENU_PAGE_SIZE:(page + 1) * MENU_PAGE_SIZE]
    lines = [f'<b>{esc(title)}</b> · {len(all_items)}',
             f'Страница {page + 1} из {pages}', '']
    keyboard = []
    if not items:
        lines.append('Заказов в этом разделе пока нет.')
    for shop_id, oid, payload, code, _ in items:
        products = payload.get('products') or []
        product = next((p.get('name') for p in products
                        if isinstance(p, dict) and p.get('name')), 'Заказ')
        lines.append(f'• <b>№{esc(oid)}</b> · {esc(status_label(code))}')
        lines.append(f'   {esc(str(product)[:52])} · {esc(local_timestamp(payload.get("createdDate")))}')
        keyboard.append([menu_button(
            f'Открыть №{oid}',
            f'km:v:{shop_id}:{oid}:{category}:{page}')])
    if pages > 1:
        paging = []
        if page > 0:
            paging.append(menu_button('◀️ Назад', f'km:{category}:{page - 1}'))
        if page + 1 < pages:
            paging.append(menu_button('Далее ▶️', f'km:{category}:{page + 1}'))
        keyboard.append(paging)
    keyboard.append([menu_button('🏠 Главное меню', 'km:h'),
                     menu_button('🔄 Обновить', f'km:{category}:{page}')])
    return '\n'.join(lines), {'inline_keyboard': keyboard}


def menu_stats():
    rows = stored_orders()
    counts = menu_counts(rows)
    labels = [('n', '🆕 Новые'), ('a', '✅ Принятые'), ('d', '🚚 На доставке'),
              ('p', '🏪 Самовывоз'), ('c', '🏁 Завершённые'), ('o', '📦 Другие')]
    lines = ['📊 <b>Статистика заказов</b>',
             f'Всего сохранено: <b>{len(rows)}</b>', '']
    for key, label in labels:
        lines.append(f'{label}: <b>{counts[key]}</b>')
    lines.append('')
    lines.append(f'Последняя синхронизация: {esc(get_config("last_sync_at") or "ещё нет")} (МСК)')
    lines.append('Данные из локальной базы; не финансовый отчёт Flowwow.')
    return '\n'.join(lines), {'inline_keyboard': [
        [menu_button('🔄 Обновить', 'km:s'), menu_button('🏠 Меню', 'km:h')]]}


def menu_last():
    row = stored_orders()
    if not row:
        text = '📭 Пока нет заказов Flowwow в локальной базе.'
        return text, {'inline_keyboard': [[menu_button('🏠 Меню', 'km:h')]]}
    shop_id, oid, payload, _, _ = row[0]
    name = get_config(f'shop_name_{shop_id}') or f'Магазин №{shop_id}'
    text = format_order(payload, name, title='🕒 <b>Последний сохранённый заказ</b>')
    return text, {'inline_keyboard': [[menu_button('🏠 Меню', 'km:h')]]}


def render_menu_callback(data):
    if data == 'km:h':
        return menu_home()
    if data == 'km:s':
        return menu_stats()
    if data == 'km:l':
        return menu_last()
    parts = data.split(':')
    if len(parts) == 3 and parts[0] == 'km' and parts[1] in MENU_CATEGORIES:
        try:
            return menu_section(parts[1], int(parts[2]))
        except ValueError:
            return menu_home()
    if len(parts) == 6 and parts[0] == 'km' and parts[1] == 'v':
        try:
            shop_id, oid = int(parts[2]), int(parts[3])
            category = parts[4]
            page = int(parts[5])
        except ValueError:
            return menu_home()
        if category not in MENU_CATEGORIES:
            return menu_home()
        with db() as c:
            item = c.execute('SELECT payload FROM orders WHERE shop_id=? AND order_id=?',
                             (shop_id, oid)).fetchone()
        if item is None:
            return 'Заказ не найден в локальной базе.', {'inline_keyboard': [
                [menu_button('◀️ К списку', f'km:{category}:{page}')]]}
        payload = json.loads(item[0])
        name = get_config(f'shop_name_{shop_id}') or f'Магазин №{shop_id}'
        return format_order(payload, name, title='🍰 <b>Карточка заказа</b>'), {
            'inline_keyboard': [[menu_button('◀️ К списку', f'km:{category}:{page}'),
                                 menu_button('🏠 Меню', 'km:h')]]}
    return menu_home()


def handle_callback(call):
    data = str(call.get('data') or '')
    ident = (call.get('message') or {}).get('chat', {}).get('id')
    kind = (call.get('message') or {}).get('chat', {}).get('type')
    message_id = (call.get('message') or {}).get('message_id')
    user_id = (call.get('from') or {}).get('id')
    owner = chat_id()
    authorized = owner is not None and user_id == owner and (
        kind == 'private' and ident == owner or
        kind in ('group', 'supergroup') and
        str(ident) == str(get_config('notify_chat_id')))
    # Only the connected owner can browse orders and details.
    if not authorized:
        tg_call('answerCallbackQuery', {
            'callback_query_id': call['id'],
            'text': 'Меню заказов доступно только владельцу бота.',
            'show_alert': False}, timeout=10)
        return
    if not data.startswith('km:'):
        tg_call('answerCallbackQuery', {'callback_query_id': call['id']}, timeout=10)
        return
    tg_call('answerCallbackQuery', {'callback_query_id': call['id']}, timeout=10)
    body, markup = render_menu_callback(data)
    if message_id is not None:
        tg_edit(ident, message_id, body, reply_markup=markup)


def configure_telegram_menu():
    commands = [
        {'command': 'menu', 'description': 'Меню заказов 🍰'},
        {'command': 'new', 'description': 'Новые заказы'},
        {'command': 'accepted', 'description': 'Принятые заказы'},
        {'command': 'delivery', 'description': 'Заказы на доставке'},
        {'command': 'completed', 'description': 'Завершённые заказы'},
        {'command': 'last', 'description': 'Последний заказ'},
        {'command': 'status', 'description': 'Состояние бота'},
        {'command': 'help', 'description': 'Список команд'},
    ]
    try:
        tg_call('setMyCommands', {'commands': commands}, timeout=15)
        tg_call('setChatMenuButton', {'menu_button': {'type': 'commands'}}, timeout=15)
        LOG.info('Telegram native menu commands configured')
    except Exception as exc:
        # Bot notifications should remain working even when UI setup fails.
        LOG.warning('Could not set Telegram menu commands: %s', type(exc).__name__)


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
            body, markup = menu_home()
            tg_send(ident, body, reply_markup=markup)
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
            tg_send(ident, '✅ Telegram подключён. Проверка новых заказов Flowwow запущена.\nОткрой меню командой /menu.')
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
    if action == '/menu':
        body, markup = menu_home()
        tg_send(ident, body, reply_markup=markup)
    elif action in ('/new', '/accepted', '/delivery', '/completed', '/pickup', '/other'):
        category = {'/new': 'n', '/accepted': 'a', '/delivery': 'd',
                    '/completed': 'c', '/pickup': 'p', '/other': 'o'}[action]
        body, markup = menu_section(category)
        tg_send(ident, body, reply_markup=markup)
    elif action == '/stats':
        body, markup = menu_stats()
        tg_send(ident, body, reply_markup=markup)
    elif action == '/status':
        with db() as c:
            shops = c.execute('SELECT COUNT(*) FROM shops_initialized').fetchone()[0]
            total = c.execute('SELECT COUNT(*) FROM orders WHERE notified=1').fetchone()[0]
            queued = c.execute('SELECT COUNT(*) FROM orders WHERE notified=0').fetchone()[0]
            pending_status = c.execute('SELECT COUNT(*) FROM status_events WHERE delivered=0').fetchone()[0]
        target = 'группа' if get_config('notify_chat_id') else 'личный чат'
        tg_send(ident, f'✅ Бот активен\nМагазинов: {shops}\nИзвестных заказов: {total}\nОжидают отправки: {queued}\nНовых событий статуса: {pending_status}\nПроверка: каждые {POLL_SECONDS} сек.\nУведомления: {target}.')
    elif action == '/last':
        latest = last_stored_order()
        if latest is None:
            tg_send(ident, '📭 Пока нет сохранённых заказов Flowwow. Дождитесь первой успешной синхронизации.')
        else:
            shop_id, order = latest
            name = get_config(f'shop_name_{shop_id}') or f'Магазин №{shop_id}'
            tg_send(ident, format_order(order, name, title='📋 <b>Последний сохранённый заказ Flowwow</b>'))
    elif action == '/teststatus':
        demo = {'id': 'TEST', 'createdDate': int(datetime.now(MOSCOW).timestamp()),
                'status': 1, 'products': [{'name': 'Тестовый бенто-торт', 'count': 1,
                                          'sum': '1000', 'currencyCode': 'RUB'}]}
        title = '🧪 <b>ТЕСТ — не настоящий заказ</b>'
        result = tg_send(ident, format_order(demo, 'Кондитерская КиТ', title=title))
        demo['status'] = 2
        tg_edit(ident, int(result['message_id']), format_order(demo, 'Кондитерская КиТ', title=title))
        tg_send(ident, '🧪 <b>Тест смены статуса</b>\n'
                       '🆕 Новый → ✅ Принят\n'
                       'Это демонстрация без запросов на изменение заказов Flowwow.')
    elif action == '/test':
        tg_send(ident, '🔔 Тестовое уведомление @zakazKiT_bot — Telegram подключён.')
    elif action == '/help':
        tg_send(ident, '/menu — открыть меню с кнопками\n/new — новые\n/accepted — принятые\n/delivery — на доставке\n/completed — завершённые\n/stats — сводка\n/last — показать последний сохранённый заказ\n'
                       '/status — состояние\n/test — проверить доставку\n/teststatus — тест редактирования карточки\n'
                       '/bindgroup — привязать группу (от владельца, в группе)\n'
                       '/unbindgroup — отвязать группу (от владельца, в группе)\n'
                       '/group — куда приходят уведомления (в личном чате)\n'
                       '/private — вернуть уведомления в личный чат\n/help — помощь')


def telegram_loop():
    offset = int(get_config('tg_offset') or 0)
    while not shutdown.is_set():
        try:
            updates = tg_call('getUpdates', {'offset': offset, 'timeout': 20, 'allowed_updates': ['message', 'callback_query']}, timeout=30)
            for u in updates:
                current = int(u['update_id'])
                try:
                    if 'callback_query' in u:
                        handle_callback(u['callback_query'])
                    else:
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
    configure_telegram_menu()
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
