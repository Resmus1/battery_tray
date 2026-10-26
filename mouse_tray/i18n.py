"""Interface language: English source strings, looked up in a Russian table.

Strings stay in English in the code and pass through :func:`tr`; with Russian
active, :func:`tr` returns the translation (or the English text if there is
none, so a missing entry never breaks the UI). ``"auto"`` follows the Windows
display language.
"""

from __future__ import annotations

import ctypes

LANGUAGES = {"auto": "Auto", "en": "English", "ru": "Русский"}

_LANG_RUSSIAN = 0x19  # primary language id of LANGID

_current = "en"

_RU = {
    # tray menu and tooltip
    "Reset timer": "Сбросить таймер",
    "Settings...": "Настройки...",
    "About...": "О программе...",
    "Exit": "Выход",
    "Auto": "Авто",
    "No Mouse Detected": "Мышь не найдена",
    "Not connected": "Не подключена",
    "Pinned mouse": "Выбранная мышь",
    "since full charge: {time}": "с полной зарядки: {time}",
    "≈ {time} left": "осталось ≈ {time}",
    "estimating time left…": "оценка времени…",
    "{d} d {h} h": "{d} д {h} ч",
    "{h} h {m} min": "{h} ч {m} мин",
    "{m} min": "{m} мин",
    "{days} days, {time}": "{days} д, {time}",
    # notifications
    "Charged 100%": "Заряжено 100%",
    "{name}: battery low ({percent}%)": "{name}: низкий заряд ({percent}%)",
    "Test: battery {percent}%": "Тест: заряд {percent}%",
    "Mouse": "Мышь",
    # settings
    "{name} {version} settings": "{name} {version} — настройки",
    "Poll interval (s):": "Интервал опроса (с):",
    "Font (monospaced):": "Шрифт (моноширинный):",
    "Font:": "Шрифт:",
    "Show all fonts:": "Все шрифты:",
    "List every installed font, not just monospaced ones.\n"
    "The digits are measured in the chosen face, so any font fits.":
        "Показать все установленные шрифты, а не только моноширинные.\n"
        "Цифры подгоняются под выбранный шрифт, так что подойдёт любой.",
    "Text size:": "Размер текста:",
    "Scale the tray digits.\n100% fills the icon; lower values shrink.":
        "Масштаб цифр в трее.\n100% заполняет значок, меньше — уменьшает.",
    "Text outline:": "Контур текста:",
    "Draw a contrasting outline around the tray digits.\n"
    "Keeps them readable on a same-colored taskbar.":
        "Контрастная обводка цифр в трее.\n"
        "Цифры читаются на панели задач того же цвета.",
    "Font color:": "Цвет шрифта:",
    "Color by charge level:": "Цвет по уровню заряда:",
    "Color the battery percent by charge: the font color\n"
    "above {mid}%, then the two colors picked next to it\n"
    "at ≤ {mid}% and ≤ {low}%.":
        "Окрашивать процент по уровню заряда: цвет шрифта\n"
        "выше {mid}%, затем два цвета, выбранные рядом,\n"
        "при ≤ {mid}% и ≤ {low}%.",
    "Show battery icon:": "Значок батарейки:",
    "Draw a battery filled to the charge level instead of\n"
    "the percent digits. The exact number moves to the\n"
    "tray tooltip.":
        "Рисовать батарейку, заполненную по уровню заряда,\n"
        "вместо цифр. Точное число — в подсказке при наведении.",
    "Low battery notification:": "Уведомление о разряде:",
    "Show a Windows notification when the charge drops to {levels}.":
        "Показывать уведомление Windows, когда заряд опускается до {levels}.",
    "Test": "Тест",
    "Show the notification now to check the Windows notification settings.":
        "Показать уведомление сейчас, чтобы проверить настройки уведомлений Windows.",
    "Notify at (%):": "Уведомлять при (%):",
    "and": "и",
    "{high}% and {low}%": "{high}% и {low}%",
    "Repeat below lower level (min):": "Повтор ниже нижнего порога (мин):",
    "While the charge stays at or below the lower level, repeat the\n"
    "notification every this many minutes. 0 turns repeats off.":
        "Пока заряд не выше нижнего порога, повторять уведомление\n"
        "каждые столько минут. 0 — без повторов.",
    "Language:": "Язык:",
    "Start with Windows:": "Запуск с Windows:",
    "Run the program when you sign in to Windows.\n"
    "Also shown on the Startup tab of Task Manager.":
        "Запускать программу при входе в Windows.\n"
        "Также видно на вкладке «Автозагрузка» Диспетчера задач.",
    "Debug logging:": "Отладочный лог:",
    "Reset to defaults": "По умолчанию",
    "Open log": "Открыть лог",
    "Open app.log -- attach it when reporting a problem.":
        "Открыть app.log — приложите его, если сообщаете о проблеме.",
    "Could not load font {face!r}.\nPlease choose another.":
        "Не удалось загрузить шрифт {face!r}.\nВыберите другой.",
    "Invalid font": "Неверный шрифт",
    "Could not change the autostart setting:\n{exc}":
        "Не удалось изменить автозапуск:\n{exc}",
    "Start with Windows": "Запуск с Windows",
    "OK": "ОК",
    "Cancel": "Отмена",
    # about
    "About {name}": "О программе {name}",
    "Supported devices ({count}):": "Поддерживаемые устройства ({count}):",
    "USB ID": "USB ID",
    "Protocol": "Протокол",
    "Universal wireless-mouse battery indicator for the Windows system tray.":
        "Универсальный индикатор заряда беспроводной мыши в системном трее Windows.",
}


def _windows_language() -> str:
    try:
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except (AttributeError, OSError):
        return "en"
    return "ru" if langid & 0x3FF == _LANG_RUSSIAN else "en"


def set_language(language: str) -> None:
    """Activate ``"en"``, ``"ru"`` or ``"auto"`` (the Windows display language)."""
    global _current  # noqa: PLW0603 -- one process-wide UI language
    _current = _windows_language() if language not in ("en", "ru") else language


def current() -> str:
    return _current


def tr(text: str, **values: object) -> str:
    """Translate ``text`` into the active language, then fill ``{}`` fields."""
    if _current == "ru":
        text = _RU.get(text, text)
    return text.format(**values) if values else text
