#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='Read notification file and print/send-ready text')
    parser.add_argument('--input', default=None, help='Notification text path (default: <report-dir>/symbols_notification.txt)')
    parser.add_argument('--report-dir', default='output/reports', help='Report dir for default input (default: output/reports)')
    parser.add_argument('--channel', default='feishu')
    parser.add_argument('--send', action='store_true', help='Reserved flag; actual sending is controlled by the pipeline config.')
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[1]

    report_dir = Path(args.report_dir)
    if not report_dir.is_absolute():
        report_dir = (base / report_dir).resolve()

    if args.input:
        input_path = Path(args.input)
        if not input_path.is_absolute():
            input_path = (base / input_path).resolve()
    else:
        input_path = (report_dir / 'symbols_notification.txt').resolve()

    text = input_path.read_text(encoding='utf-8').strip() if input_path.exists() else ''
    print(text or '今日无需要主动提醒的内容。')


if __name__ == '__main__':
    main()
