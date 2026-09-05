// Display vocabulary only. Business records always come from the API.
export const PRIORITIES: Record<string, string> = { high: 'Высокий', medium: 'Средний', low: 'Низкий' }
export const TYPES: Record<string, string> = { news: 'Новости', npa: 'НПА' }
export const SOURCE_TYPES: Record<string, string> = { rss: 'RSS', telegram: 'Telegram', sitemap: 'Sitemap', html: 'Сайт', search: 'Поиск', manual: 'Вручную' }
export const SOURCE_STATUSES: Record<string, string> = { active: 'Активен', paused: 'Пауза', error: 'Ошибка', deleted: 'Удалён' }
export const INTERVALS: Record<string, string> = { '15m': '15 минут', '1h': '1 час', '6h': '6 часов', '24h': '24 часа' }
export const CATEGORIES = ['регуляторика', 'репутация', 'конкуренты', 'тренды', 'господдержка/льготы']
export const NPA_STATUSES = ['анонс', 'разработка', 'внесён', 'рассмотрение', 'принят', 'действует', 'архив']
export const EDIT_REASONS: Record<string, string> = { hallucination: 'Неточность ИИ', wrong_focus: 'Изменение акцента', wrong_priority: 'Неверный приоритет', other: 'Другое' }
export const VISIBILITY: Record<string, string> = { visible: 'В ленте', hidden_feed: 'Скрыт из ленты', hidden_digest: 'Исключён из дайджеста', deleted: 'Удалён' }
export const ENTITY_ROLES: Record<string, string> = { who: 'Кто', what: 'Что', when: 'Когда', impact: 'Последствия', org: 'Организация', act_number: 'Номер акта' }
