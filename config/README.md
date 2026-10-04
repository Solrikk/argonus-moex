# Настройки

`forward_shadow_manifest.json` и `delayed_entry_shadow_manifest.json` задают
теневые эксперименты. Их контрольные суммы привязаны к опубликованному коду.

Файлы `*activation_manifest.example.json` — неактивные шаблоны. Поля `approved`
и `production_activation_allowed` имеют значение `false`, контрольные суммы
заменены шаблонами. Копирование или переименование примера не активирует стратегию.
Рабочие `*activation_manifest.json` исключены из Git.
