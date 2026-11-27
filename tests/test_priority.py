import unittest

from types import SimpleNamespace
from unittest.mock import patch

from gitarchive.priority import (
    CREATE_NO_WINDOW_FLAG,
    CPU_PRIORITY_CLASSES,
    MEMORY_PRIORITY_VALUES,
    PROCESS_MEMORY_PRIORITY_CLASS,
    memory_priority_value,
    normalize_cpu_priority,
    normalize_memory_priority,
    process_creation_flags,
    hide_qprocess_console,
)


class BackgroundPriorityTests(unittest.TestCase):
    def test_cpu_priority_levels_match_windows_priority_classes(self):
        self.assertEqual(CPU_PRIORITY_CLASSES["idle"], 0x00000040)
        self.assertEqual(CPU_PRIORITY_CLASSES["below_normal"], 0x00004000)
        self.assertEqual(CPU_PRIORITY_CLASSES["normal"], 0x00000020)

    def test_memory_priority_information_class_uses_windows_enum_value_zero(self):
        self.assertEqual(PROCESS_MEMORY_PRIORITY_CLASS, 0)

    def test_memory_priority_levels_match_windows_values(self):
        self.assertEqual(MEMORY_PRIORITY_VALUES["very_low"], 1)
        self.assertEqual(MEMORY_PRIORITY_VALUES["low"], 2)
        self.assertEqual(MEMORY_PRIORITY_VALUES["normal"], 5)
        self.assertEqual(memory_priority_value("very_low"), 1)

    def test_priority_values_are_normalized(self):
        self.assertEqual(normalize_cpu_priority("IDLE"), "idle")
        self.assertEqual(normalize_cpu_priority("invalid"), "idle")
        self.assertEqual(normalize_memory_priority("LOW"), "low")
        self.assertEqual(normalize_memory_priority("invalid"), "very_low")

    def test_creation_flags_preserve_selected_windows_cpu_class(self):
        for name, priority_class in CPU_PRIORITY_CLASSES.items():
            flags = process_creation_flags(name, hide_console=False)
            if __import__("os").name == "nt":
                self.assertEqual(flags & priority_class, priority_class)
                self.assertEqual(flags & CREATE_NO_WINDOW_FLAG, 0)
            else:
                self.assertEqual(flags, 0)

    def test_default_windows_creation_flags_hide_console(self):
        import os
        for name in CPU_PRIORITY_CLASSES:
            flags = process_creation_flags(name)
            if os.name == "nt":
                self.assertEqual(flags & CREATE_NO_WINDOW_FLAG, CREATE_NO_WINDOW_FLAG)
                self.assertNotEqual(flags & CPU_PRIORITY_CLASSES[name], 0)
            else:
                self.assertEqual(flags, 0)

    def test_qprocess_modifier_adds_create_no_window(self):
        class FakeQProcess:
            def setCreateProcessArgumentsModifier(self, modifier):
                self.modifier = modifier

        process = FakeQProcess()
        with patch("gitarchive.priority.os.name", "nt"):
            self.assertTrue(hide_qprocess_console(process))

        args = SimpleNamespace(flags=0x00000020)
        process.modifier(args)
        self.assertEqual(args.flags, 0x00000020 | CREATE_NO_WINDOW_FLAG)


if __name__ == "__main__":
    unittest.main()
