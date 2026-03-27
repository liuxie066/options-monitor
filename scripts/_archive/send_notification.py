#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='Read notification file and print/send-ready text')
    parser.add_argument('--input', default='output/reports/symbols_notification.txt')
    parser.add_argument('--channel', default='feishu')
    parser.add_argument('--send', action='store_true', help='Reserved flag; actual sending is controlled by the pipeline config.')
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[1]
    input_path = base / args.input
    text = input_path.read_text(encoding='utf-8').strip() if input_path.exists() else ''
    print(text or '今日无需要主动提醒的内容。')


if __name__ == '__main__':
    main()
