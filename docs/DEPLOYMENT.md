# Развёртывание

## Текущий сервер

| Параметр | Значение |
| --- | --- |
| Каталог | `/opt/alisa-schedule` |
| Compose project | `alisa-schedule` |
| Контейнер | `alisa-schedule-skill-1` |
| Внутренний адрес | `http://127.0.0.1:18791` |
| Webhook | `https://kkepik.rub1kub.ru/alice/webhook` |

На сервере работают бот ККЭПик, его веб-приложение и другие сервисы. У навыка свой каталог и контейнер. Не меняйте файлы, структуру и настройки существующих проектов. Для расписания используются публичные GET-маршруты API.

## Первый запуск

Передайте исходники в каталог проекта. Создайте `.env` из примера, задайте `ALICE_SKILL_ID` из консоли Яндекса и оставьте `APP_ENV=production`.

```bash
cd /opt/alisa-schedule
cp .env.example .env
# Заполните ALICE_SKILL_ID в .env.
docker compose build
docker compose run --rm --no-deps skill python -m scripts.validate_data
docker compose up -d --wait
curl --fail http://127.0.0.1:18791/healthz
curl --fail http://127.0.0.1:18791/readyz
```

На действующем сервере `.env` уже заполнен; при обновлении его сохраняйте. ID и адрес карточки записаны в `deploy/skill-card.json`. ID используется для маршрутизации и не является секретом.

Runtime-зависимости зафиксированы в `requirements.lock`. Базовый образ — `python:3.12-slim`; его содержимое может меняться при пересборке. Контейнер ограничен 192 MiB памяти и половиной CPU.

## HTTPS на существующем домене

Маршрут `/alice/webhook` подключается отдельным файлом Apache. Он принимает только HTTPS POST с Host `kkepik.rub1kub.ru` и передаёт запрос в контейнер.

```bash
install -m 0644 deploy/apache-existing-domain.conf /etc/apache2/conf-available/alisa-schedule.conf
a2enconf alisa-schedule
apache2ctl configtest
systemctl reload apache2
```

Reload выполняйте после успешного configtest. Существующий virtual host остаётся у веб-приложения. Для отключения маршрута: `a2disconf alisa-schedule`, configtest и reload. [Проксирование Apache](https://httpd.apache.org/docs/2.4/mod/mod_proxy.html#proxypass)

Проверьте HTTPS из каталога локальной копии:

```bash
.venv/bin/python -m scripts.smoke --url https://kkepik.rub1kub.ru --path /alice/webhook --skill-id YOUR_SKILL_ID
```

GET по адресу webhook отклоняется. Проверки процесса `/healthz` и готовности `/readyz` доступны на loopback.

Для отдельного домена есть шаблоны `deploy/apache-http.conf.example` и `deploy/apache-https.conf.example`. Замените `ALICE_DOMAIN`, направьте DNS на свой сервер и получите сертификат через webroot `/var/www/alisa-schedule`. На сервере с занятым портом 80 используйте существующий Apache для ACME challenge. Проверьте продление сертификата и configtest перед reload.

## Яндекс Диалоги

1. Создайте публичный навык в [консоли](https://dialogs.yandex.ru/developer/). Заполните название, описание, пример запуска, категорию и иконку.
2. Укажите Webhook URL `https://kkepik.rub1kub.ru/alice/webhook`.
3. Включите «Использовать хранилище данных в навыке». Оно сохраняет группу и предпочтение студента между запусками.
4. Запишите ID навыка в `.env` и запустите контейнер в production-режиме.
5. Проверьте выбор группы, завтра, первую пару, количество, смену формата, выход и новый запуск. Повторите для первого и старшего курса, гостя и авторизованного пользователя.
6. Отправьте карточку на модерацию. После одобрения нажмите «Опубликовать»; навык появится в каталоге через 5–10 минут. [Публикация](https://yandex.ru/dev/dialogs/alice/doc/ru/publication)

Экран для работы не требуется. Голосовые команды разбираются по тексту и стандартной сущности `YANDEX.DATETIME`. Произношения групп настраиваются в `college.json`. [Настройки публикации](https://yandex.ru/dev/dialogs/alice/doc/ru/publish-settings)

### Приватный навык для Станции

Для тестирования используется отдельный «ККЭПик тест» с тем же webhook. Его ID задаётся в `ALICE_TEST_SKILL_ID`; основной `ALICE_SKILL_ID` остаётся обязательным. Backend принимает только эти два ID. Пустой `ALICE_TEST_SKILL_ID` отключает тестовый навык на стороне backend.

После изменения `.env` выполните `docker compose up -d --wait`. Оба навыка используют один кэш и общий лимит обращений к API. Группа и формат ответа сохраняются Яндексом отдельно для каждого навыка.

В карточке тестового навыка выберите приватный доступ, включите хранилище и опубликуйте его. Станция должна быть привязана к тому же аккаунту Яндекса. Команда запуска: **«Алиса, запусти навык ККЭПик тест»**. Затем назовите группу и спросите «что завтра». [Тестирование](https://yandex.ru/dev/dialogs/alice/doc/ru/test)

## Обновление

### Публичная страница проекта

`https://kkepik.rub1kub.ru/about` обслуживается контейнером навыка. HTML лежит в `app/static/about.html`; при его изменении пересоберите образ. Доступ без авторизации, внешних скриптов и шрифтов. Главная страница Telegram-приложения остаётся на `/`.

Маршруты лендинга и публичного файла подтверждения Вебмастера заданы отдельно в `deploy/apache-brand.conf`, установленном как `/etc/apache2/conf-available/alisa-brand.conf`. После установки выполните `a2enconf alisa-brand`, `apache2ctl configtest` и только при успешной проверке `systemctl reload apache2`. Лендинг разрешает GET/HEAD по HTTP и HTTPS на `/about` и `/about/`, включая параметры запроса. Файл Вебмастера остаётся на точном HTTPS-пути. Все маршруты ограничены доменом проекта; текущий virtual host и маршрут webhook не меняются.

HTML-файл Вебмастера в `app/static` должен оставаться доступным для повторных проверок. Это публичное подтверждение владения сайтом, не пароль или ключ API. Для отката этого выпуска отключите `alisa-brand` через `a2disconf`, проверьте конфигурацию, перезагрузите Apache и восстановите предыдущий образ `alisa-schedule:before-brand-20260917` вместе с `app/main.py` из `/opt/alisa-schedule/before-brand-20260917.tar.gz`.

После изменения маршрутов выполните `python -m scripts.check_landing`: проверяются HTTP/HTTPS, путь со слешем и без него, параметры запроса и GET/HEAD через публичный Apache. Локальные тесты FastAPI не заменяют эту проверку. В Яндекс Вебмастере дополнительно проверьте `/about/` инструментом «Проверка ответа сервера» — это запрос со стороны Яндекса.

### Код и данные

Для кода: проверьте изменения локально, загрузите исходники, соберите образ и пересоздайте контейнер навыка. Сохраняйте действующие `.env` и локальные файлы данных. Предыдущий образ нужен для отката.

```bash
docker compose build
docker compose run --rm --no-deps skill python -m scripts.validate_data
docker compose up -d --wait
```

Пересоздание одного контейнера даёт короткий перерыв в обработке webhook. Он наблюдался при первой проверке обновления. Для переключения без перерыва нужны два экземпляра и общий бюджет обращений к API.

Для текстов редактируйте `data/responses.json`, проверьте данные и выполните `docker compose restart skill`. Пересборка не требуется. Правила и параметры описаны в [RESPONSES.md](RESPONSES.md).

Замены обновляются без перезапуска. Загрузите подготовленный файл в `data/overrides.local.json`:

```bash
docker compose run --rm --no-deps -e OVERRIDES_FILE=/app/data/overrides.local.json skill python -m scripts.validate_data
cp data/overrides.json data/overrides.previous.local.json
mv data/overrides.local.json data/overrides.json
```

Заменяйте активный файл только после успешной проверки. Одна запись должна содержать весь день; пустой список занятий отменяет пары. Каталог `data` в контейнере доступен только для чтения, поэтому публикация выполняется на хосте. Права файла — `0644`. Для отката проверьте и опубликуйте резервную копию тем же способом.

## Диагностика

`/healthz` проверяет процесс. `/readyz` проверяет наличие каталога и валидность замен; отсутствие расписания на конкретную дату не означает сбой сервиса.

`docker compose logs --tail=50 skill` показывает типы ошибок без текстов запросов, пользовательских ID и ответов API. Размер логов ограничен. Автообновление контейнера через Watchtower отключено.

Текущие проверки и ограничения перечислены в [VERIFICATION.md](VERIFICATION.md). Время первой пары берётся из таблицы звонков в `college.json`; при изменении звонков обновите её и перезапустите навык.
