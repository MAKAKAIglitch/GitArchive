import unittest

from gitarchive.i18n import LANGUAGE, detect_system_language, tr


class LocalizationTests(unittest.TestCase):
    def test_detected_language_is_supported(self):
        self.assertIn(detect_system_language(), {"en", "ru"})
        self.assertIn(LANGUAGE, {"en", "ru"})

    def test_english_translation(self):
        self.assertEqual(tr("Обзор", "en"), "Overview")
        self.assertEqual(tr("Настройки", "en"), "Settings")
        self.assertEqual(tr("Принимать подключения", "en"), "Accept incoming connections")

    def test_russian_is_preserved(self):
        self.assertEqual(tr("Обзор", "ru"), "Обзор")
        self.assertEqual(tr("Принимать подключения", "ru"), "Принимать подключения")

    def test_saved_english_status_can_switch_back_to_russian(self):
        self.assertEqual(tr("Ready", "ru"), "Готово")
        self.assertEqual(tr("Synchronizing", "ru"), "Синхронизация")
        self.assertEqual(tr("No Git output", "ru"), "Нет вывода от Git")
        self.assertEqual(
            tr(
                "Git has produced no output for more than 3 minutes. The process is still running; "
                "it may be waiting on the network, disk, or local object processing.",
                "ru",
            ),
            "Git не отправлял вывод более 3 минут. Процесс ещё работает; это может быть ожидание сети, диска или локальная обработка объектов.",
        )

    def test_repository_url_errors_and_provider_labels_are_translated(self):
        self.assertEqual(
            tr("Вставь HTTPS- или SSH-ссылку на Git-репозиторий.", "en"),
            "Enter an HTTPS or SSH URL for a Git repository.",
        )
        self.assertEqual(tr("Свой Git-сервер", "en"), "Custom Git server")

    def test_dynamic_text_is_translated(self):
        self.assertEqual(
            tr("История коммитов — example/repo", "en"),
            "Commit history — example/repo",
        )
        self.assertEqual(
            tr("Убран из списка (архив сохранён): example/repo", "en"),
            "Removed from list (archive kept): example/repo",
        )
        self.assertEqual(tr("1.5 ТБ", "en"), "1.5 TB")
        self.assertEqual(
            tr("Передача завершена: owner/repo (12 MB).", "en"),
            "Transfer completed: owner/repo (12 MB).",
        )

    def test_heartbeat_text_is_translated_before_formatting(self):
        template = tr("Синхронизация всё ещё выполняется. Прошло {} с; новых данных от Git нет {} с.", "en")
        self.assertEqual(
            template.format(240, 180),
            "Synchronization is still running. Elapsed: 240 s; no new Git output for 180 s.",
        )


if __name__ == "__main__":
    unittest.main()
