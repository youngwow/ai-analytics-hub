# Invalid run

Этот прогон не используется в метриках: capture transport повторно передавал `Content-Encoding` уже декодированного тела и некорректно читал streaming request body. Ошибка исправлена до следующего запуска.
