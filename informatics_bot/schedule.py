"""Reader for daily DOCX timetables: classes in columns, lesson numbers at left."""
import argparse
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from docx import Document
from docx.table import Table

CLASSES = ('7А', '7Б', '7В', '7Г', '7Д', '7З', '8А', '8Б', '8Г', '8З', '8Е')
CLASS = re.compile(r'^\s*([5789])\s*[«"\-]?\s*([а-яё])(?=\s|[»"]|$)', re.I)
SUBJECT = re.compile(r'(?<![а-яё])ин\s*ф(?:орм(?:атика)?)?(?=$|[^а-яё])', re.I)
NUMBER = re.compile(r'^\s*(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?\s*$')


@dataclass(frozen=True)
class Lesson:
    school_class: str
    number: str
    subject: str


def read_schedule(path: str | Path):
    # Check expanded size before python-docx opens the archive.
    with zipfile.ZipFile(path) as archive:
        if sum(item.file_size for item in archive.infolist()) > 30 * 1024 * 1024:
            raise ValueError('Слишком большой DOCX после распаковки (лимит 30 МБ).')
    document = Document(path)
    found, present = [], set()
    for block in document.iter_inner_content():
        if not isinstance(block, Table):
            continue
        columns = {}
        for row in block.rows:
            cells = [''] * row.grid_cols_before + [cell.text.strip() for cell in row.cells]
            headers = {}
            for index, value in enumerate(cells):
                match = CLASS.match(value)
                if match:
                    headers[index] = ''.join(match.groups()).upper()
            if headers:
                columns = headers
                present.update(c for c in columns.values() if c in CLASSES)
                continue
            for index, school_class in columns.items():
                if school_class not in CLASSES or index >= len(cells) or not SUBJECT.search(cells[index]):
                    continue
                # Each shift has its own number column. Use the closest one to the left.
                number = '?'
                for left in range(index - 1, -1, -1):
                    if left not in columns and NUMBER.fullmatch(cells[left]):
                        number = re.sub(r'\s+', '', cells[left]).replace('–', '-').replace('—', '-')
                        break
                lesson = Lesson(school_class, number, ' '.join(cells[index].split()))
                if lesson not in found:
                    found.append(lesson)
    return found, present


def report(path: str | Path, today: date, bells: dict) -> str:
    lessons, present = read_schedule(path)
    if not present:
        return 'Не найдены столбцы нужных классов. Ожидается таблица: номер урока слева, классы в заголовках.'
    if not lessons:
        return 'В присланном расписании нет уроков информатики для выбранных классов.'
    lines = [f'Информатика из присланного файла (обработан {today:%d.%m.%Y}):']
    missing_time = False
    # In the second shift, 1-7 is the seventh lesson of the whole day.
    lessons.sort(key=lambda item: (
        int(item.number.split('-')[-1]) if item.number != '?' else float('inf'),
        CLASSES.index(item.school_class),
    ))
    for item in lessons:
        time = bells.get(item.number)
        missing_time |= not bool(time)
        number = item.number if item.number != '?' else 'не определён'
        lines.append(f'{item.school_class}: урок {number}, {time or "время не настроено"} — {item.subject}')
    if missing_time:
        lines.append('\nТочное время появится после заполнения bells.json. Обозначения 1-7, 2-8 и т. п. сохранены из файла.')
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Проверить расписание без Telegram')
    parser.add_argument('file', type=Path)
    parser.add_argument('--date', type=date.fromisoformat)
    parser.add_argument('--timezone', default='Asia/Krasnoyarsk')
    args = parser.parse_args()
    bells = json.loads(Path(__file__).with_name('bells.json').read_text(encoding='utf-8'))
    print(report(args.file, args.date or datetime.now(ZoneInfo(args.timezone)).date(), bells))
