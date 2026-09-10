import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from schedule import report

BASE = Path(__file__).resolve().parent
MAX_BYTES = 10 * 1024 * 1024


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        'Отправьте DOCX с расписанием. Я покажу информатику для '
        '7А, 7Б, 7В, 7Г, 7Д, 7З и 8А, 8Б, 8Г, 8З, 8Е. '
        'Классы должны быть в столбцах таблицы. Дата файла не проверяется. '
        'Занятия по подгруппам показываются с исходным названием предмета.')


async def receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    attachment = message.document
    if not (attachment.file_name or '').lower().endswith('.docx'):
        await message.reply_text('Нужен файл .docx. Файлы .doc и PDF не поддерживаются.')
        return
    if attachment.file_size and attachment.file_size > MAX_BYTES:
        await message.reply_text('Файл слишком большой. Лимит — 10 МБ.')
        return
    try:
        with TemporaryDirectory(prefix='informatics-') as folder:
            path = Path(folder) / 'schedule.docx'
            remote = await attachment.get_file()
            await remote.download_to_drive(path)
            if path.stat().st_size > MAX_BYTES:
                raise ValueError('Файл слишком большой. Лимит — 10 МБ.')
            today = datetime.now(context.application.bot_data['timezone']).date()
            answer = await asyncio.to_thread(report, path, today, context.application.bot_data['bells'])
    except ValueError as error:
        answer = f'Не удалось прочитать расписание: {error}'
    except Exception:
        logging.getLogger(__name__).warning('Ошибка загрузки или чтения DOCX')
        answer = 'Не удалось загрузить или прочитать файл. Проверьте, что это исправный DOCX с таблицами, и попробуйте снова.'
    # Telegram limits text messages to 4096 characters.
    for offset in range(0, len(answer), 3500):
        await message.reply_text(answer[offset:offset + 3500])


def main():
    load_dotenv(BASE / '.env')
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        raise SystemExit('Укажите TELEGRAM_BOT_TOKEN в файле .env (см. .env.example).')
    timezone = ZoneInfo(os.getenv('TIMEZONE', 'Asia/Krasnoyarsk'))
    bells = json.loads((BASE / 'bells.json').read_text(encoding='utf-8'))
    if not isinstance(bells, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in bells.items()):
        raise SystemExit('bells.json должен содержать объект с номерами уроков и временем в виде строк.')
    application = Application.builder().token(token).build()
    application.bot_data.update(timezone=timezone, bells=bells)
    application.add_handler(CommandHandler(['start', 'help'], start))
    application.add_handler(MessageHandler(filters.Document.ALL, receive))
    application.run_polling()


if __name__ == '__main__':
    main()
