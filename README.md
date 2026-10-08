# Regional Map Framework

Самостоятельный каркас интерактивных региональных аналитических карт. Он не
привязан к бренду, отрасли или фиксированному списку объектов: в каждый регион
можно добавить произвольные GeoJSON-слои и собственную аналитику.

Репозиторий содержит **код и структуру, но не региональные данные**. Данные
хранятся отдельно и подключаются к региону одной командой.

## Возможности

- любое число регионов с общей неизменяемой картой;
- точки, линии, полигоны и смешанные предметные модели;
- декларативные popup, подписи, стили и контроль полей;
- renderer-режимы `points`, `lines`, `polygons`, `choropleth`, `density` и `ranking`;
- декларативные легенды, рейтинговые таблицы и переход к объекту или связанной сетке;
- выгрузка таблиц в Excel (.xlsx): числовые ячейки, фильтры, закреплённые заголовки и отдельный лист источников;
- адаптивный мобильный интерфейс карты с отдельными панелями слоёв, легенды, объектов и информации;
- внешняя папка данных без копирования в Git;
- региональные воспроизводимые pipelines с input lock и manifest;
- транзакционная публикация с отдельным publication manifest;
- строгие `doctor`, `promote`, `sync` и обнаружение изменения внешнего snapshot;
- проверка геометрий, координат, схемы свойств и контрольных количеств;
- отсутствие скрытой зависимости от родительского проекта.

## Требования

- Python 3.10 или новее (Linux, macOS или Windows);
- современный браузер;
- интернет для Leaflet, MapLibre и векторной подложки OpenFreeMap (данные OpenStreetMap);
- дополнительные зависимости только для конкретных региональных pipelines.

Production-развёртывание на Linux описано в [DEPLOYMENT.md](DEPLOYMENT.md).
В репозитории есть проверяемые шаблоны environment-файла, `systemd` и Nginx;
региональные конфигурации и данные устанавливаются отдельным deployment-пакетом.
Ручной production-CD через GitHub Actions использует отдельный forced-command
SSH-ключ, разворачивает точный commit SHA и автоматически откатывает неуспешный
release; настройка описана в разделе 8 deployment-инструкции.
На сервере его можно полностью вынести из Git-клона через
`RMF_CONTENT_ROOT=/srv/regional-map-deployment`.
Изменяемое состояние регионов аналогично выносится через
`RMF_RUNTIME_ROOT=/var/lib/regional-map-framework/regions`.

## Быстрый старт

```bash
git clone <repository-url> regional-map-framework
cd regional-map-framework
python3 manage.py verify-core

python3 manage.py init-region my_region \
  --title "Мой регион" --center-lat 55.75 --center-lon 37.62

python3 manage.py add-layer my_region \
  --layer-id pharmacies --file pharmacies.geojson \
  --label "Аптеки" --geometry Point --renderer points
```

Подготовьте внешний каталог:

```text
/srv/regional-map-data/my_region/
└── pharmacies.geojson
```

Подключите его без копирования:

```bash
python3 manage.py attach-data my_region \
  --data-dir /srv/regional-map-data/my_region --attach-mode symlink
python3 manage.py validate my_region
python3 manage.py auth-set-user map_admin
python3 serve.py --bind 127.0.0.1 --port 8000
```

После входа открывается главная страница с галереей всех регионов из
`registry.json`:

```text
http://localhost:8000/
```

Прямая ссылка на карту:

```text
http://localhost:8000/map.html?region=my_region
```

На экранах шириной до 820 px карта автоматически использует мобильный режим:
десктопные контролы заменяются нижней навигацией и выдвижной панелью. Карта,
состояние слоёв и региональные контракты при этом остаются общими для обоих
режимов; отдельная мобильная копия данных не создаётся.

После подключения framework записывает в `.runtime/publication.json` SHA-256
всех слоёв. Если внешний snapshot изменился, `validate` завершится ошибкой. Для
осознанного принятия новой версии выполните `accept-data`.
При заданном `RMF_RUNTIME_ROOT` этот manifest хранится вне регионального пакета
в `<RMF_RUNTIME_ROOT>/<region>/publication.json`.

Режимы подключения: `symlink` для Linux/macOS и Windows Developer Mode,
`junction` для обычной Windows, `copy` для переносимого локального snapshot.
Команда `detach-data` удаляет только ссылку/junction; скопированные данные она
намеренно не удаляет.

```bash
python3 manage.py detach-data my_region
```

## Формат GeoJSON

Каждый файл является `FeatureCollection`. Минимальный точечный слой:

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {"type": "Point", "coordinates": [37.62, 55.75]},
      "properties": {"name": "Объект", "address": "Адрес"}
    }
  ]
}
```

После `add-layer` отредактируйте описание слоя в `region.json`. Например:

```json
{
  "file": "data/pharmacies.geojson",
  "label": "Аптеки",
  "renderer": "points",
  "geometry_types": ["Point"],
  "required_properties": ["name", "address"],
  "numeric_properties": [],
  "required_for_production": false,
  "title_field": "name",
  "popup_fields": [
    "address",
    {"field": "source_url", "label": "Источник", "type": "url"}
  ],
  "style": {"color": "#16a34a", "radius": 6}
}
```

## Два режима данных

### Готовые внешние данные

Создайте регион с `--data-mode external` (значение по умолчанию) и используйте
`attach-data`. Это самый простой режим для локальной работы и закрытых наборов.
Snapshot всё равно контролируется publication manifest, хотя происхождение
документируется вручную в `SOURCES.md`.

### Воспроизводимая сборка

Создайте регион с `--data-mode pipeline`. `add-layer` автоматически добавляет
соответствующий вложенный output. Затем опишите входы и шаги в
`regions/<id>/pipeline/pipeline.json`. Входные
файлы помещаются в `sources/` и также не коммитятся. После принятия input lock:

```bash
python3 pipeline_core/runner.py \
  --region-root regions/my_region --write-lock
python3 manage.py build my_region
```

Pipeline пишет только в staging, проверяет полный набор выходов и атомарно
заменяет `data/` после успешной сборки. `sync` разрешает публикацию только когда
runner manifest, declared outputs и все SHA-256 согласованы.

## Аналитические модели

Предметная аналитика остаётся подключаемым региональным plugin, а не частью
renderer. Контракт находится в `templates/analytics-plugin.example.json`:
модель объявляет входы, выход, версию, методологию, параметры и quality controls.
Рабочая декларация помещается в `regions/<id>/pipeline/plugins/`. Её команда
включается в `pipeline.json`, поэтому код, конфигурация и входы
попадают в input lock и manifest.

Для локальной плотности доступен общий построитель
`pipeline_core/build_adaptive_density.py`. Он сохраняет контрольную численность
населённого пункта, распределяет её по прокси жилой площади OSM и рекурсивно
уменьшает ячейки только в сложной городской застройке. Разреженные территории
остаются крупнее. Пример параметров находится в
`templates/adaptive-density.example.json`. Результат является моделью, а не
официальной квартальной статистикой.

## Доступ по логину и паролю

Карты, региональные конфигурации, GeoJSON и API закрыты обязательной серверной
сессией. Публичны только страница входа и `/healthz`. Регистрации, восстановления
и изменения пароля через сайт нет.

Первого пользователя создаёт администратор в консоли VPS:

```bash
python3 manage.py auth-set-user map_admin
```

Пароль вводится дважды интерактивно и не попадает в shell history. Минимальная
длина — 12 UTF-8 байт. Управление пользователями также выполняется только из
консоли:

```bash
python3 manage.py auth-list-users
python3 manage.py auth-set-user analyst     # создать или сменить пароль
python3 manage.py auth-delete-user analyst
```

При смене пароля все действующие сессии этого пользователя сразу отзываются.
Пароли хранятся как PBKDF2-HMAC-SHA256 с индивидуальной солью. По умолчанию
credential store находится в `.runtime/users.json`, игнорируется Git и создаётся
с правами `0600`. Для постоянного внешнего хранилища задайте один и тот же путь
CLI и сервису:

```bash
export RMF_AUTH_FILE=/srv/regional-map-secrets/users.json
```

Сессия живёт 12 часов; срок можно задать через `RMF_SESSION_SECONDS` в пределах
от 5 минут до 7 дней. На VPS обязательно используйте HTTPS на reverse proxy и
запускайте backend с `RMF_COOKIE_SECURE=1`. Backend откажется слушать
нелокальный адрес без Secure-cookie; `RMF_ALLOW_INSECURE_HTTP=1` существует
только для изолированной разработки и не должен использоваться на VPS.
Если rate limiting должен учитывать реальный IP за reverse proxy, задайте
`RMF_TRUST_PROXY=1` только когда backend
недоступен напрямую, а proxy сам перезаписывает `X-Forwarded-For`.
Файл пользователей необходимо включить в закрытую резервную копию: он содержит
хеши и секрет подписи сессий, но не должен попадать в Git или публичный artifact.

После входа HTTP-сервер также применяет строгий белый список файлов. Он отдаёт
только страницы галереи и карты (`index.html`, `map.html`), объявленные
клиентские ресурсы `core/`, `registry.json`, `region.json` зарегистрированных
регионов и GeoJSON, явно указанные слоями соответствующего
`region.json`. Исходный код, `.git`, `.runtime`, pipeline, `SOURCES.md` и любые
необъявленные файлы через HTTP не публикуются. Это правило действует одинаково
для `GET` и `HEAD`; reverse proxy не должен обходить backend и самостоятельно
раздавать корень репозитория.

## Публичная перегенерация

Повторяемые открытые слои можно обновлять отдельно от передаваемых вручную
snapshot. Пометьте слой в `region.json` полем `"regenerable": true`, создайте
`pipeline/regeneration.json` по примеру
`templates/regeneration.example.json` и пишите результат команды в каталог из
переменной `RMF_OUTPUT_DIR`. Framework копирует текущую публикацию в staging,
обновляет только объявленные файлы, валидирует все слои и атомарно публикует
результат. Минимальный серверный cooldown принудительно равен 300 секундам.

HTTP-запуск намеренно выключен по умолчанию. Для локальной проверки:

```bash
RMF_ALLOW_REGENERATION=1 python3 serve.py --bind 127.0.0.1 --port 8000
```

Кнопка и `/api/regeneration` доступны только вошедшим пользователям и только для
регионов из `registry.json`; сам запуск дополнительно требует серверного флага
`RMF_ALLOW_REGENERATION=1`. Перегенерация требует обычного локального каталога `data/`;
symlink на внешнее хранилище отвергается, чтобы атомарная замена не затронула
чужой каталог. Время и результат последнего запуска находятся только в
игнорируемом `.runtime/regeneration.json`. При заданном `RMF_RUNTIME_ROOT` эти
файлы находятся в приватном внешнем runtime, а не внутри регионального пакета.

`pipeline/regeneration.json` является доверенной серверной конфигурацией: его
команды не формируются из HTTP-параметров и не должны быть доступны пользователю
для изменения. На VPS запускайте сервер от отдельного непривилегированного
пользователя с записью только в каталоги данных и runtime. Вывод последнего
запуска сохраняется с правами `0600` в `.runtime/regeneration.log`. Команда
ограничена одним часом; значение можно изменить переменной
`RMF_REGENERATION_TIMEOUT_SECONDS` в безопасном диапазоне от 60 секунд до 24
часов. Lock содержит PID процесса и автоматически снимается при следующем
запросе, если процесс аварийно завершился. После успешной публикации открытая
карта перезагружается сама.

## Проверка production

```bash
python3 manage.py doctor my_region
python3 manage.py promote my_region
```

Повышение запрещено при отсутствующих/пустых обязательных слоях, неполном
`SOURCES.md`, шаблонном source note, несовпадении manifest или ненастроенном
pipeline.

## Deployment-пакет без данных

После перевода всех регионов в `production` соберите переносимый пакет
контрактов. Каталог назначения должен отсутствовать — существующий пакет команда
никогда не перезаписывает:

```bash
python3 manage.py deployment-check
python3 manage.py bundle-build --bundle-dir deployment/release/contracts
python3 manage.py bundle-verify --bundle-dir deployment/release/contracts
```

В пакет входят `registry.json`, региональные конфигурации, документация
источников, pipeline-файлы и `deployment-manifest.json`. GeoJSON, source inputs,
кэши и outputs не копируются. Manifest содержит SHA-256 и размер каждого
контракта, а также ожидаемые SHA-256 всех отдельных файлов данных.

Если данные подготовлены в структуре `<data-root>/<region>/<file>.geojson`, их
можно проверить перед отправкой:

```bash
python3 manage.py bundle-verify \
  --bundle-dir deployment/release/contracts \
  --data-dir deployment/release/data
```

Каталог `deployment/` игнорируется Git.

## Что разрешено коммитить

- общий код, схемы и шаблоны;
- `region.json`, `pipeline.json`, региональные адаптеры и документацию;
- `SOURCES.md` с происхождением данных;
- небольшие тестовые данные только при явном разрешении их лицензии.

Не коммитьте PBF, реальные каталоги организаций, пользовательские выгрузки,
pipeline outputs, кэши и документы без права распространения. `.gitignore`
защищает типовые тяжёлые файлы, но ответственность за лицензию остаётся у
автора регионального пакета.

## Команды

| Команда | Назначение |
|---|---|
| `init-region` | Создать пустой регион и зарегистрировать его |
| `add-layer` | Добавить декларацию произвольного слоя |
| `attach-data` | Подключить внешний каталог как `data/` |
| `accept-data` | Принять изменившийся внешний snapshot и обновить SHA-256 manifest |
| `detach-data` | Удалить ссылку, не изменяя внешние файлы |
| `validate` | Проверить конфигурацию и данные региона |
| `doctor` | Показать полный список препятствий production |
| `validate-all` | Проверить все зарегистрированные регионы |
| `deployment-check` | Fail-fast проверка непустого production deployment-пакета |
| `bundle-build` | Собрать проверяемый пакет контрактов без региональных данных |
| `bundle-verify` | Проверить контракты и опционально отдельный каталог данных |
| `build` | Выполнить региональный pipeline и опубликовать результаты |
| `sync` | Опубликовать уже проверенные outputs без пересчёта |
| `promote` | Транзакционно перевести готовый draft в production |
| `self-test` | Проверить input lock, manifest, rollback и защиту путей |
| `verify-core` | Проверить неизменность общего каркаса |

Подробный контракт находится в [REGION_CONTRACT.md](REGION_CONTRACT.md), правила
источников — в [DATA_SOURCES.md](DATA_SOURCES.md), renderer-конфигурация — в
[`templates/layer.example.json`](templates/layer.example.json).

## Каталог региональных партнёров

Опциональный модуль ведёт справочник публичных деловых контактов агентств
недвижимости, брокеров, риэлторов, девелоперов и управляющих компаний. Сбор
создаёт только набор кандидатов; опубликованный каталог меняется после просмотра
дельты и отдельного подтверждения администратора. Страница региона доступна по
`/contacts.html?region=<region_id>`. Настройка источников, CLI, ограничения
автоматического сбора и production-переменные описаны в [CONTACTS.md](CONTACTS.md).
