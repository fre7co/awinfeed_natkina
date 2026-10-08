# NATKINA → Awin: бесплатный полный фид

Скрипт берёт **весь** публичный каталог `natkina.com/products.json` (все страницы по 250) и собирает:

- `public/awin.csv`: фид в формате Awin CSV, одна строка на вариант, включая распроданные (`in_stock=0`);
- `public/google.xml`: тот же фид в формате Google Shopping;
- `public/report.txt`: что в данных Shopify сломано и что скрипт подставил сам.

Нужен только Python 3, внешних библиотек нет.

## 1. Проверка у себя (5 мин)
```
python natkina_feed.py
```
Открой `public/report.txt`. В первой строке будет число товаров и вариантов, его нужно сравнить с Shopify Admin → Products.
Перед запуском проверь в Shopify → Settings → Store currency, что валюта CHF. Если нет, поменяй `CURRENCY` в начале скрипта.

## 2. Хостинг с автообновлением (бесплатно, 15 мин)
1. Создай на GitHub **публичный** репозиторий, например `natkina-feed`, и загрузи в него эту папку целиком вместе с `.github/workflows/feed.yml`.
2. Открой Settings → Pages → Source и выбери **GitHub Actions**.
3. Открой Actions → «Build NATKINA feed» → **Run workflow**.
4. Фид появится по адресу `https://<твой-логин>.github.io/natkina-feed/awin.csv`. Дальше он обновляется каждые 6 часов.

Важно: GitHub отключает расписание, если в репозитории 60 дней не было коммитов. Раз в 1–2 месяца достаточно сделать любой коммит или нажать Run workflow.

## 3. Подключение в Awin
Toolbox → My Product Feeds → добавить фид → Transfer method **HTTP(S)**, URL из пункта 2, формат **CSV**, разделитель «запятая», кодировка UTF-8, расписание до 4 раз в день.

Колонки сопоставляются в шаге Column mapping:

| Колонка фида | Поле Awin |
|---|---|
| product_id | Product ID |
| product_name | Product Name |
| description | Description |
| merchant_deep_link | Deep Link |
| merchant_image_url | Image URL |
| search_price | Search Price (price) |
| rrp_price | RRP Price |
| currency | Currency |
| in_stock | In Stock |
| brand_name | Brand Name |
| mpn | MPN / Model Number |
| merchant_category | Merchant Category, плюс Category Mapping в Awin: Rings / Earrings / Necklaces → Jewellery |
| colour, size, material | Colour, Size, Material |
| parent_product_id | Parent Product ID |

## Что скрипт исправляет сам
- Пустое описание заменяется сгенерированным: название, тип, металл, камень, размер.
- Если у варианта нет своего фото, берётся главное фото товара.
- Vendor «Natkina» и «NATKINA» сводится к одному написанию «NATKINA».
- Опции Metal Color / Stone Color / Size раскладываются в material / colour / size.
- Если старая цена выше текущей, она попадает в rrp_price (Google: price + sale_price).

## Что скрипт НЕ может исправить (это делается в Shopify, список в report.txt)
- Пустые описания: лучше написать настоящие тексты.
- Варианты без SKU.
- Опечатки в размерах, например кольцо с размером «5» вместо «54».
- Товары без фото: они исключаются из фида.
- EAN/GTIN: у NATKINA их нет, и в `products.json` они не публикуются. Товар идентифицируется через бренд + MPN (SKU).
