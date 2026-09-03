# PR-менеджеры (репутация, конкуренты, инфоповоды, тренды) — 28 источников

## PR (rss) — 9

| url                                               | name          | Источник | Защита страниц                             | в json      |
| ------------------------------------------------- | ------------- | -------- | ------------------------------------------ | ----------- |
| https://www.cnews.ru/inc/rss/biz.xml              | CNews         | rss      | нет                                        | да          |
| https://www.cnews.ru/inc/rss/telecom.xml          | CNews Телеком | rss      | нет                                        | да          |
| https://www.kommersant.ru/RSS/news.xml            | Коммерсантъ   | rss      | частично                | да          |
| https://rssexport.rbc.ru/rbcnews/news/30/full.rss | РБК           | rss      | да            | да (off)    |
| https://tass.ru/rss/v2.xml                        | ТАСС          | rss      | да | да (off)          |
| https://lenta.ru/rss/news                         | Lenta.ru      | rss      | нет                                        | да          |
| https://www.interfax.ru/rss.asp                   | Интерфакс     | rss      | нет                                        | да          |
| https://d-russia.ru/feed                          | D-Russia      | rss      | нет                                        | да          |
| https://habr.com/ru/rss/news/                     | Habr          | rss      | нет                                        | да (?fl=ru) |

## PR (tg) — 12

| url                       | name                               | Источник | в json            |
| ------------------------- | ---------------------------------- | -------- | ----------------- |
| https://t.me/CNewsDaily   | CNews                              | tg       | да  |
| https://t.me/tadviser     | TAdviser                           | tg       | да  |
| https://t.me/comnewsgroup | ComNews                            | tg       | да  |
| https://t.me/cablemanru   | Кабельщик                          | tg       | да  |
| https://t.me/telesputnik  | Телеспутник                        | tg       | да  |
| https://t.me/rspectr      | RSpectr                            | tg       | да  |
| https://t.me/icipr        | ЦИПР                               | tg       | да   |
| https://t.me/cio_channel  | CIO Channel                        | tg       | да  |
| https://t.me/tass_agency  | ТАСС                               | tg       | да  |
| https://t.me/kommersant   | Коммерсантъ                        | tg       | да  |
| https://t.me/vedomosti    | Ведомости                          | tg       | да  |
| https://t.me/cit_gov      | Цифровые индустриальные технологии | tg       | да    |

## PR (llm-поиск) — 7

| запрос                                                          | --domains                               | --days | --category | --general | --name                       | Источник  | в json |
| --------------------------------------------------------------- | --------------------------------------- | ------ | ---------- | --------- | ---------------------------- | --------- | ------ |
| GS Labs OR Триколор OR StingrayTV                               | vedomosti.ru                            | 7      | media      | —         | Ведомости — технологии       | llm-поиск | нет    |
| GS Labs OR Триколор OR DRE Advanced Security OR условный доступ | telesputnik.ru                          | 7      | media      | —         | Телеспутник — упоминания     | llm-поиск | нет    |
| GS Labs OR Триколор OR операторы платного ТВ                    | broadcasting.ru                         | 7      | media      | —         | Broadcasting.ru — упоминания | llm-поиск | нет    |
| Ростелеком Wink OR платное ТВ OR ТВ-приставка                   | company.rt.ru,rt.ru                     | 7      | media      | да        | Ростелеком — пресс-центр     | llm-поиск | нет    |
| МТС KION OR медиасервисы OR платное ТВ                          | ir.mts.ru,mts.ru                        | 7      | media      | да        | МТС / KION — пресс-центр     | llm-поиск | нет    |
| GS Labs OR Триколор OR StingrayTV                               | vc.ru                                   | 14     | media      | да        | VC.ru — упоминания           | llm-поиск | нет    |
| отзывы сотрудников GS Labs OR ГС Лабс                           | dreamjob.ru,hh.ru,pravda-sotrudnikov.ru | 30     | media      | да        | HR-площадки — отзывы         | llm-поиск | нет    |

---

# GR-специалисты (НПА, регуляторы, законотворчество, точки роста) — 30 источника

## GR (открытые API) — 5

| url                                                    | name                           | Источник            | в json                           |
| ------------------------------------------------------ | ------------------------------ | ------------------- | -------------------------------- |
| http://publication.pravo.gov.ru/help                   | Оф. портал правовой информации | открытое API (REST) | да, но как rss (3 ленты api/rss) |
| https://regulation.gov.ru/opendata/7710349494-npalist/ | regulation.gov.ru (Open Data)  | открытое API        | в excluded                       |
| https://zakupki.gov.ru/epz/opendata/                   | ЕИС Закупки                    | открытое API        | нет                              |
| https://api-fns.ru / egrul.nalog.ru                    | ФНС / ЕГРЮЛ                    | открытое API        | нет                              |
| http://api.duma.gov.ru                                 | АСОЗД Госдумы (API)            | открытое API ⚠ ключ | нет                              |

## GR (rss) — 5

| url                                                      | name              | Источник                        | Защита страниц                   | в json                                                   |
| -------------------------------------------------------- | ----------------- | ------------------------------- | -------------------------------- | -------------------------------------------------------- |
| http://publication.pravo.gov.ru/api/rss?block=government | pravo.gov.ru      | rss                             | нет                              | да (3 ленты: government, federal_authorities, president) |
| http://government.ru/all/rss/                            | Правительство РФ  | rss                             | нет                              | да                                                       |
| https://www.consultant.ru/rss/hotdocs.xml                | КонсультантПлюс   | rss                             | нет                              | да                                                       |
| https://rss.garant.ru/categories/news/                   | Гарант            | rss                             | нет                              | да                                                       |
| https://www.cbr.ru/rss/RssNews                           | ЦБ РФ             | rss                             | нет | да (off)                                     |

## GR (прямой парсинг) — 8

| url                                                                          | name                      | Источник                   | в json                                 |
| ---------------------------------------------------------------------------- | ------------------------- | -------------------------- | -------------------------------------- |
| https://digital.gov.ru/ru/events/                                            | Минцифры России           | прямой парсинг + llm-поиск | в excluded (есть канал t.me/mintsifry) |
| https://sozd.duma.gov.ru/oz                                                  | СОЗД Госдумы              | прямой парсинг + llm-поиск | в excluded                             |
| https://reestr.digital.gov.ru/reestr/                                        | Реестр отечественного ПО  | прямой парсинг             | в excluded                             |
| https://rkn.gov.ru/news/                                                     | Роскомнадзор              | прямой парсинг             | да (rss /_services/rss/)               |
| https://fstec.ru/normotvorcheskaya/informacionnye-i-analiticheskie-materialy | ФСТЭК России              | прямой парсинг             | в excluded                             |
| http://www.fsb.ru/fsb/science/single.htm                                     | ФСБ (лицензирование/СКЗИ) | прямой парсинг             | да (html)                              |
| https://www.nalog.gov.ru/rn77/news/                                          | ФНС России                | прямой парсинг             | да (rss /rn77/rss/)                    |
| https://minpromtorg.gov.ru/press-centre/news/                                | Минпромторг               | прямой парсинг             | нет (есть канал Торгпред)              |

## GR (tg) — 8

| url                         | name                       | Источник | в json            |
| --------------------------- | -------------------------- | -------- | ----------------- |
| https://t.me/mintsifry      | Минцифры России            | tg       | да   |
| https://t.me/government_rus | Правительство РФ           | tg       | да |
| https://t.me/duma_gov_ru    | Госдума                    | tg       | да  |
| https://t.me/arppsoft       | АРПП «Отечественный софт»  | tg       | да   |
| https://t.me/arperf         | АРПЭ                       | tg       | да |
| https://t.me/rfrit          | РФРИТ                      | tg       | да  |
| https://t.me/DataEconomyRU  | АНО «Цифровая экономика»   | tg       | да  |
| https://t.me/fasietalks     | Фонд содействия инновациям | tg       | да   |

## GR (llm-поиск) — 4

| запрос                                                                        | --domains                    | --days | --category | --general | --name                          | Источник  | в json                   |
| ----------------------------------------------------------------------------- | ---------------------------- | ------ | ---------- | --------- | ------------------------------- | --------- | ------------------------ |
| законопроект персональные данные OR КИИ OR реестр отечественного ПО           | sozd.duma.gov.ru,duma.gov.ru | 14     | regulator  | да        | СОЗД — законопроекты по ПО и ПД | llm-поиск | да (сводный запрос, off) |
| отзыв ассоциации на законопроект OR позиция АРПП OR АРПЭ                      | arpp.ru,arpe.ru,raec.ru      | 14     | regulator  | да        | Ассоциации — позиции по НПА     | llm-поиск | нет                      |
| ФАС решение OR судебная практика лицензирование ПО OR антимонопольное дело ИТ | fas.gov.ru,kad.arbitr.ru     | 30     | regulator  | да        | ФАС и судебная практика         | llm-поиск | нет                      |
| меры поддержки ИТ-компаний регион OR субсидия OR налоговая льгота             | —                            | 30     | regulator  | да        | Меры поддержки в регионах       | llm-поиск | нет                      |

---

# Руководители подразделений — 5

Роль потребляющая: своих источников почти нет, нужен агрегат поверх PR+GR.

| url                                                  | name                          | Источник                                   | Что берём                                         | в json                 |
| ---------------------------------------------------- | ----------------------------- | ------------------------------------------ | ------------------------------------------------- | ---------------------- |
| (внутренний) `/digest/weekly`                        | Еженедельный сводный дайджест | агрегат PR+GR                              | Топ-10 за неделю, только «высокий» приоритет      | —                      |
| (внутренний) `/watchlist/npa`                        | Трекер отслеживаемых НПА      | агрегат (pravo.gov.ru + СОЗД + regulation) | Статусы законопроектов и их изменения             | —                      |
| https://t.me/mintsifry + https://t.me/government_rus | Госповестка (сжатая)          | tg                                         | Только материалы с прямым эффектом на компанию    | да                     |
| https://www.cnews.ru/inc/rss/biz.xml + TAdviser      | Рыночный фон                  | rss                                        | Конкуренты и крупные сделки, фильтр «эффект есть» | да                     |
| (внутренний) `/alerts/critical`                      | Молнии                        | агрегат + llm-приоритизация                | Приказы/требования с дедлайном комплаянса         | —                      |
| https://t.me/grantsforbussines                       | Гранты для ИТ                 | tg                                         | Доп.                                              | нет (есть в базе, #11) |
| https://t.me/rustorgpred                             | Торгпред                      | tg                                         | Доп.                                              | да (off)               |
