"""Inspect only the recent cat drawing's requests, statuses and error evidence."""

import json
import sqlite3


with sqlite3.connect('file:/app/backend/data/webui.db?mode=ro', uri=True) as database:
    rows = database.execute('SELECT id,title,chat FROM chat WHERE chat LIKE ? ORDER BY updated_at DESC LIMIT 2',
                            ('%cat_doodle.svg%',)).fetchall()
for identifier, title, raw in rows:
    chat = json.loads(raw)
    messages = list((chat.get('history') or {}).get('messages', {}).values())
    print(json.dumps({'chat_id': identifier, 'title': title, 'messages': [
        {'id': message.get('id'), 'role': message.get('role'), 'content': (message.get('content') or '')[:2500],
         'error': message.get('error'), 'statusHistory': message.get('statusHistory'),
         'output': message.get('output', [])} for message in messages[-3:]]}, ensure_ascii=False))
