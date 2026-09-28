# Contributing

1. Не добавляйте реальные или тяжёлые данные в pull request.
2. Изменение общего core требует обновления lock только после review:
   `python3 manage.py verify-core --write-core-lock`.
3. Новый регион должен иметь `SOURCES.md` и проходить `doctor` без ошибок
   структуры.
4. Новая предметная модель должна оставаться региональным модулем или общим
   opt-in plugin и не вводить обязательные отраслевые поля в core.
5. Перед commit выполните `python3 -m unittest discover -s tests`,
   `python3 manage.py self-test` и `python3 manage.py verify-core`.
