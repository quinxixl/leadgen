#!/bin/zsh
cd "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  echo 'Сначала установите окружение по инструкции README.md.'
  read '?Нажмите Enter, чтобы закрыть окно.'
  exit 1
fi
if [[ ! -f .env ]]; then
  .venv/bin/python -m leadgen.app setup || { read '?Нажмите Enter, чтобы закрыть окно.'; exit 1; }
fi
.venv/bin/python -m leadgen.app bot
read '?Нажмите Enter, чтобы закрыть окно.'
