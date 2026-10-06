# Обновление Приморского края на VPS

06.10.2026, выпуск `20261006T012850Z`, VPS `51.15.251.13`.
Новый набор установлен и подключён к работающему серверу.
Код сервера не менялся: `c59a4417730bce8e4081978ac87d10a6f81d0f59`.

## Бэкап

На VPS: `/srv/regional-map-backups/primkrai/20261006T012850Z/`.
Архив: `primkrai-backup.tar.gz`.
SHA256: `d616f28a182445fedc85297a9f54bb04f7847d9f7610fce5e26c39a8c2e33f94`.

Сохранены независимые копии старого контракта, семи файлов данных и runtime
Приморья, а также справочная копия реестра. Архив проверен через пробное
извлечение и сравнение всех файлов по SHA256; ссылки проверены по метаданным.
После переключения исходная папка `/srv/regional-map-data/primkrai` осталась
побайтно неизменной. Старый действовавший контракт дополнительно сохранён
как `previous-active-contract/`.

Дополнительная локальная копия архива:
`deployment/primkrai-current-20261006/vps-previous-backup.tar.gz`.
Её SHA256 совпадает с проверенным архивом VPS.

## Активный набор

- Контракт: `/srv/regional-map-deployment/regions/primkrai/region.json`.
- Данные: `/srv/regional-map-data/releases/primkrai/20261006T012850Z/`.
- Регионный `data` указывает на эту новую версию.
- Runtime: `/var/lib/regional-map-framework/regions/primkrai/`.
- Пакет загрузки: `/srv/regional-map-staging/primkrai/20261006T012850Z/`.

| Слой | Число объектов, подтверждённое HTTP |
|---|---:|
| Муниципалитеты | 33 |
| Населённые пункты | 647 |
| Карточки Пятёрочки | 170 |
| Приоритет открытий | 68 |
| Основные дороги | 5 764 |
| Федеральные дороги | 807 |
| Сетка с официальными контролями населения | 41 921 |
| Сетка с неподтверждёнными контролями | 1 299 |
| Жильё: очередь проверки v3 | 6 566 |
| Чувствительность плотности | 43 220 |

Приоритеты пересчитаны по свежему каталогу; исправлено сопоставление
Горных Ключей. Восемь неизвестных значений населения показаны как `null`,
а не нулевое население. Свежие жилищные сведения не меняли веса плотности.
Полные исходники и реестр 411 886 зданий остаются локально: на сервер
перенесены все слои карты и сопроводительные документы, а не архив исходников.

## Проверки после установки

- Проверен SHA256 загруженного архива; пакет проверен действующим кодом VPS.
- До и после переключения общий `deployment-check` выполнен от имени
  `regional-map` и прошёл для всех пяти регионов.
- Сервис перезапущен и находится в состоянии `active`; `/healthz` — `ok`.
- Страница `/map.html?region=primkrai`, активный контракт и все десять
  GeoJSON запрошены у работающего HTTP-сервера с временной сессией в памяти.
  Все ответы — HTTP 200, контрольные суммы данных совпадают с файлами.
  Учётные записи и файл аутентификации не менялись; токен не сохранялся.
- Реестр, его default_region=buryatia и контракты четырёх остальных регионов
  сохранены без изменений. Их данные не заменялись.
- Старая локальная папка analytical_maps не изменялась. Пакет и бэкап
  исключены из Git/GitHub.

Записи на VPS: `backup-verification.json`, `deployment-result.json`,
`pre-switch-deployment-check.log`, `post-switch-deployment-check.log`,
`http-verification.json` в папке бэкапа.
Локальные записи: `backup-result.json`, `vps-stage-verification.log`,
`vps-deployment-result.json`, `vps-http-verification.json` в папке пакета.

## Откат этого выпуска

Откат возвращает только Приморье. Не восстанавливать весь registry.json:
он сохранён исключительно как справочная копия. Команды ниже применимы,
пока этот выпуск остаётся активным и не было последующих обновлений.
Перед откатом проверить активную цель регионного `data`.

```bash
readlink -f /srv/regional-map-deployment/regions/primkrai/data
systemctl stop regional-map-framework
mv /srv/regional-map-deployment/regions/primkrai \
  /srv/regional-map-backups/primkrai/20261006T012850Z/rolled-back-new-contract
mv /srv/regional-map-backups/primkrai/20261006T012850Z/previous-active-contract \
  /srv/regional-map-deployment/regions/primkrai
cp -p /srv/regional-map-backups/primkrai/20261006T012850Z/runtime/publication.json \
  /var/lib/regional-map-framework/regions/primkrai/publication.json
chown regional-map:regional-map \
  /var/lib/regional-map-framework/regions/primkrai/publication.json
runuser -u regional-map -- env \
  RMF_CONTENT_ROOT=/srv/regional-map-deployment \
  RMF_RUNTIME_ROOT=/var/lib/regional-map-framework/regions \
  /opt/regional-map-framework/current/.venv/bin/python -B \
  /opt/regional-map-framework/current/manage.py deployment-check
systemctl start regional-map-framework
curl -fsS http://127.0.0.1:8000/healthz
```

Контроли населения остаются 2021/2025 годов, OSM — 08.09.2026;
каталог магазинов — 06.10.2026. Заселённость, кадастровая привязка зданий
и текущая работа магазинов независимо не подтверждены.
