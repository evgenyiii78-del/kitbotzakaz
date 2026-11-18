import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main as bot


def order(oid=101, status=1):
    return {
        'id': oid, 'status': status, 'createdDate': 1763350000,
        'deliveryDateFrom': 1763450000,
        'products': [{'name': 'Тестовый торт', 'count': 1, 'sum': '1000',
                      'currencyCode': 'RUB', 'selectedProperties': []}],
    }


class OrderTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_path = bot.DATABASE_PATH
        bot.DATABASE_PATH = Path(self.tmp.name) / 'orders.db'
        self.addCleanup(setattr, bot, 'DATABASE_PATH', self.old_path)
        bot.init_db()
        with bot.db() as conn:
            conn.execute('INSERT INTO shops_initialized(shop_id) VALUES(1)')

    def add_existing(self, status=1, notified=1, oid=101, chat=None, mid=None):
        payload = json.dumps(order(oid, status))
        with bot.db() as conn:
            conn.execute('''INSERT INTO orders
                (shop_id,order_id,payload,notified,status_code,message_id,message_chat_id)
                VALUES (1,?,?,?,?,?,?)''', (oid, payload, notified, status, mid, chat))

    def test_new_order_notified_once_and_message_id_saved(self):
        bot.sync_shop_orders(1, {101: order()})
        with mock.patch.object(bot, 'tg_send', return_value={'message_id': 777}) as send:
            bot.deliver_shop_updates(1, 'КиТ', -100001)
            bot.deliver_shop_updates(1, 'КиТ', -100001)
        self.assertEqual(send.call_count, 1)
        with bot.db() as conn:
            self.assertEqual(conn.execute(
                'SELECT notified,message_id,message_chat_id FROM orders WHERE order_id=101'
            ).fetchone(), (1, 777, -100001))

    def test_status_change_edits_card_and_sends_one_alert(self):
        self.add_existing(status=1, chat=-100001, mid=55)
        bot.sync_shop_orders(1, {101: order(status=2)})
        bot.sync_shop_orders(1, {101: order(status=2)})
        with bot.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM status_events').fetchone()[0], 1)
        with mock.patch.object(bot, 'tg_edit', return_value={}) as edit, mock.patch.object(
                bot, 'tg_send', return_value={'message_id': 99}) as send:
            bot.deliver_shop_updates(1, 'КиТ', -100001)
            bot.deliver_shop_updates(1, 'КиТ', -100001)
        self.assertEqual(edit.call_count, 1)
        self.assertEqual(send.call_count, 1)
        self.assertIn('Принят', edit.call_args.args[2])
        self.assertIn('Новый', send.call_args.args[1])
        with bot.db() as conn:
            self.assertEqual(conn.execute('SELECT delivered FROM status_events').fetchone()[0], 1)

    def test_old_legacy_order_without_message_gets_new_card_at_next_change(self):
        payload = json.dumps(order(status=1))
        with bot.db() as conn:
            conn.execute('INSERT INTO orders(shop_id,order_id,payload,notified) VALUES(1,101,?,1)', (payload,))
        bot.sync_shop_orders(1, {101: order(status=4)})
        with mock.patch.object(bot, 'tg_send', return_value={'message_id': 33}) as send:
            bot.deliver_shop_updates(1, 'КиТ', 100)
        self.assertEqual(send.call_count, 2)  # card + status event
        with bot.db() as conn:
            self.assertEqual(conn.execute('SELECT message_id FROM orders').fetchone()[0], 33)

    def test_first_sync_does_not_replay_existing_orders(self):
        with bot.db() as conn:
            conn.execute('DELETE FROM shops_initialized WHERE shop_id=1')
        self.assertTrue(bot.sync_shop_orders(1, {101: order()}))
        with bot.db() as conn:
            self.assertEqual(conn.execute('SELECT notified FROM orders').fetchone()[0], 1)
        with mock.patch.object(bot, 'tg_send') as send:
            bot.deliver_shop_updates(1, 'КиТ', -100001)
            send.assert_not_called()

    def test_migration_keeps_existing_orders(self):
        with bot.db() as conn:
            conn.execute('DROP TABLE orders')
            conn.execute('''CREATE TABLE orders(shop_id INTEGER,order_id INTEGER,payload TEXT,
                           notified INTEGER, PRIMARY KEY(shop_id,order_id))''')
            conn.execute('INSERT INTO orders VALUES(1,101,?,1)', (json.dumps(order()),))
        bot.init_db()
        with bot.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 1)
            cols = {row[1] for row in conn.execute('PRAGMA table_info(orders)')}
            self.assertTrue({'status_code', 'message_id', 'message_chat_id'} <= cols)

    def test_old_unfinished_orders_are_checked(self):
        self.add_existing(status=2)
        with mock.patch.object(bot, 'fetch_order_detail', return_value=order(status=4)) as fetch:
            fresh = {}
            bot.refresh_stored_orders(1, fresh)
        fetch.assert_called_once_with(1, 101)
        self.assertEqual(fresh[101]['status'], 4)

    def test_completed_orders_are_not_rechecked(self):
        self.add_existing(status=3)
        with mock.patch.object(bot, 'fetch_order_detail') as fetch:
            bot.refresh_stored_orders(1, {})
        fetch.assert_not_called()

    def test_telegram_failure_keeps_pending_event(self):
        self.add_existing(status=1, chat=-10001, mid=1)
        bot.sync_shop_orders(1, {101: order(status=2)})
        with mock.patch.object(bot, 'tg_edit', side_effect=ConnectionError('Telegram unavailable')):
            bot.deliver_shop_updates(1, 'КиТ', -10001)
        with bot.db() as conn:
            self.assertEqual(conn.execute('SELECT delivered FROM status_events').fetchone()[0], 0)
        with mock.patch.object(bot, 'tg_edit', return_value={}), mock.patch.object(
                bot, 'tg_send', return_value={'message_id': 2}):
            bot.deliver_shop_updates(1, 'КиТ', -10001)
        with bot.db() as conn:
            self.assertEqual(conn.execute('SELECT delivered FROM status_events').fetchone()[0], 1)

    def test_route_switch_recreates_card_in_new_group(self):
        self.add_existing(status=1, chat=100, mid=10)
        bot.sync_shop_orders(1, {101: order(status=4)})
        with mock.patch.object(bot, 'tg_edit') as edit, mock.patch.object(
                bot, 'tg_send', return_value={'message_id': 222}) as send:
            bot.deliver_shop_updates(1, 'КиТ', 200)
        edit.assert_not_called()
        self.assertEqual(send.call_count, 2)
        with bot.db() as conn:
            self.assertEqual(conn.execute(
                'SELECT message_chat_id,message_id FROM orders').fetchone(), (200, 222))

    def test_teststatus_is_telegram_only(self):
        with mock.patch.object(bot, 'chat_id', return_value=55), mock.patch.object(
                bot, 'tg_send', return_value={'message_id': 444}) as send, mock.patch.object(
                bot, 'tg_edit', return_value={}) as edit, mock.patch.object(
                bot, 'flowwow') as flowwow:
            bot.handle_update({'message': {
                'chat': {'type': 'private', 'id': 55},
                'from': {'id': 55}, 'text': '/teststatus'}})
        self.assertEqual(send.call_count, 2)
        edit.assert_called_once()
        flowwow.assert_not_called()


    def test_menu_categories_and_filtering(self):
        cases = [(101, 1, 'n'), (102, 2, 'a'), (103, 5, 'a'),
                 (104, 4, 'd'), (105, 11, 'd'), (106, 3, 'c'),
                 (107, 7, 'p'), (108, 99, 'o')]
        for oid, status, _ in cases:
            self.add_existing(status=status, oid=oid)
        counts = bot.menu_counts(bot.stored_orders())
        self.assertEqual(counts, {'n': 1, 'a': 2, 'd': 2, 'p': 1, 'c': 1, 'o': 1})
        for oid, status, category in cases:
            body, markup = bot.menu_section(category)
            self.assertIn('Открыть', str(markup))
            self.assertIn(f'№{oid}', body)
        home, markup = bot.menu_home()
        self.assertIn('Новые', str(markup))
        self.assertIn('На доставке', str(markup))
        self.assertIn('Принятые', str(markup))
        self.assertIn('Всего в базе: <b>8</b>', home)

    def test_menu_callback_owner_can_navigate_and_view_order(self):
        self.add_existing(status=2, oid=432, chat=-10011)
        bot.put_config('notify_chat_id', -10011)
        query = {'id': 'callback-1', 'data': 'km:a:0',
                 'from': {'id': 55}, 'message': {'message_id': 7,
                     'chat': {'id': -10011, 'type': 'supergroup'}}}
        with mock.patch.object(bot, 'chat_id', return_value=55), mock.patch.object(
                bot, 'tg_call', return_value=True) as send_api, mock.patch.object(
                bot, 'tg_edit', return_value={}) as edit:
            bot.handle_callback(query)
            self.assertIn('Принятые', edit.call_args.args[2])
            self.assertEqual(send_api.call_args.args[0], 'answerCallbackQuery')
            query['data'] = 'km:v:1:432:a:0'
            bot.handle_callback(query)
            self.assertIn('№ 432', edit.call_args.args[2])

    def test_menu_callback_denies_other_group_member(self):
        self.add_existing(status=2)
        bot.put_config('notify_chat_id', -10011)
        query = {'id': 'callback-2', 'data': 'km:a:0',
                 'from': {'id': 99}, 'message': {'message_id': 7,
                     'chat': {'id': -10011, 'type': 'supergroup'}}}
        with mock.patch.object(bot, 'chat_id', return_value=55), mock.patch.object(
                bot, 'tg_call', return_value=True) as send_api, mock.patch.object(
                bot, 'tg_edit', return_value={}) as edit:
            bot.handle_callback(query)
            edit.assert_not_called()
            self.assertIn('только владельцу', send_api.call_args.args[1]['text'])

    def test_menu_command_in_private_chat(self):
        self.add_existing(status=4)
        with mock.patch.object(bot, 'chat_id', return_value=55), mock.patch.object(
                bot, 'tg_send', return_value={'message_id': 7}) as send:
            bot.handle_update({'message': {'chat': {'id': 55, 'type': 'private'},
                                           'from': {'id': 55}, 'text': '/menu'}})
            self.assertIn('inline_keyboard', send.call_args.kwargs['reply_markup'])
            self.assertIn('Кондитерская КиТ', send.call_args.args[1])

    def test_menu_pages_and_order_details(self):
        for i in range(13):
            self.add_existing(status=1, oid=100 + i)
        body, markup = bot.menu_section('n', 1)
        self.assertIn('Страница 2 из 3', body)
        self.assertEqual(len(markup['inline_keyboard']), 8)  # 6 items, pager, navigation
        body, markup = bot.render_menu_callback('km:v:1:102:n:0')
        self.assertIn('№ 102', body)
        self.assertIn('К списку', str(markup))

    def test_configure_telegram_menu_calls_correct_methods(self):
        with mock.patch.object(bot, 'tg_call', return_value=True) as api:
            bot.configure_telegram_menu()
        self.assertEqual([x.args[0] for x in api.call_args_list],
                         ['setMyCommands', 'setChatMenuButton'])
        commands = api.call_args_list[0].args[1]['commands']
        self.assertIn('menu', [c['command'] for c in commands])


if __name__ == '__main__':
    unittest.main()
