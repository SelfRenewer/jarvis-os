"""Router eval set: phrases the user says (Telegram, Mac, phone calls) → the expected action(s).

Built 2026-10-14 from the help text, the feature list, test phrases and real usage,
rewritten in the loose spoken style of voice notes and calls. Jarvis keeps no message texts, so these are curated,
not logged. `expect` lists acceptable intents for each action in order («a|b» = either); `kind` groups the report.
Run: `uv run python scripts/eval_router.py` (live OpenRouter calls: about $0.002 per LLM-routed phrase).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    text: str
    expect: tuple[str, ...]
    kind: str = "command"


def c(text: str, *expect: str, kind: str = "command") -> Case:
    return Case(text, expect, kind)


CASES: list[Case] = [
    # --- agenda / today ---
    c("что у меня сегодня", "show_today"),
    c("что у меня завтра?", "show_agenda"),
    c("какие планы на выходные", "show_agenda"),
    c("слушай а что там у меня в пятницу по встречам", "show_agenda"),
    c("расскажи мне про мой день", "show_today|show_agenda"),
    c("есть что-нибудь важное на этой неделе", "show_agenda|show_focus"),
    c("чем мне сейчас заняться", "suggest_next"),
    c("на чём сфокусироваться сегодня", "show_focus|suggest_next"),
    c("покажи задачи", "show_tasks"),
    c("какие у меня задачи на сегодня", "show_tasks|show_today|show_agenda"),
    # --- tasks ---
    c("напомни завтра в 10 позвонить в банк", "remind|add_task"),
    c("добавь задачу купить подарок маме до пятницы", "add_task"),
    c("мне надо не забыть записаться к стоматологу", "add_task|remind"),
    c("напомни через 10 минут проверить почту", "remind|mac_remind|add_task"),
    c("перенеси стрижку на завтра", "schedule_referenced_task"),
    c("сдвинь встречу с Ниной на час позже", "schedule_referenced_task"),
    c("перенеси это на понедельник", "schedule_referenced_task"),
    c("я постригся", "complete_task"),
    c("готово купить хлеб", "complete_task"),
    c("отметь что я позвонил маме", "complete_task"),
    c("удали задачу купить сигару", "delete_task"),
    c("сделай стрижку срочной", "set_priority"),
    c("перенеси все просроченные на завтра", "bulk_tasks"),
    c("закрой все задачи на сегодня", "bulk_tasks"),
    c("составь план на день", "plan"),
    c("когда мне выходить на встречу", "leave_time"),
    c("я дома", "origin_reset"),
    c("добавь в постричься подзадачу записаться к барберу", "add_task"),
    c("найди всё про врача", "search_all"),
    # --- tracker TLT / TPL ---
    c("спал 7:30 качество 85 оценка 7 настроение хорошее", "tracker_log"),
    c("сегодня день на восьмёрку", "tracker_log"),
    c("вчера была тренировка и холодный душ", "tracker_log"),
    c("как я спал на неделе", "tracker_show"),
    c("средняя оценка за месяц", "tracker_show"),
    c("покажи дневник", "diary"),
    c("заполни день", "diary_fill"),
    c("TPL отметь утреннюю рутину и чтение", "tpl_mark"),
    c("что по сферам в этом месяце", "tpl_show"),
    c("талия 92", "tracker_log"),
    # --- pills ---
    c("выпил утренние", "med_taken"),
    c("я сегодня утром выпил D3 6000 и омегу две капсулы", "med_taken"),
    c("напоминай пить магний 400 мг в 21:00 курсом 8 недель", "med_add"),
    c("утренние таблетки: D3 6000, цинк 25 в 9:00", "med_pack"),
    c("мои лекарства", "med_list"),
    c("больше не напоминай про магний", "med_remove"),
    c("продли магний на две недели", "med_extend"),
    c("выпил энергетик", "med_taken|med_portion"),
    # --- food ---
    c("съел 200 грамм гречки и два яйца", "food_log"),
    c("на обед был борщ", "food_log"),
    c("сколько я сегодня съел", "food_show"),
    c("норма 2200 ккал белок 140", "food_targets"),
    c("удали гречку из еды", "food_remove"),
    c("выпил магний и съел 200 грамм творога", "med_taken+food_log|food_log|med_taken"),
    # --- health / labs ---
    c("какой у меня пульс", "health_show"),
    c("сколько я прошёл сегодня", "health_show"),
    c("мой вес", "health_show"),
    c("мои анализы", "labs_show"),
    c("какой ферритин в анализах", "labs_show"),
    # --- money ---
    c("потратил 45 лари на такси", "expense_add"),
    c("сколько я потратил в октябре", "expense_show"),
    c("удали последний расход", "expense_remove"),
    c("сколько будет 15% от 2000", "calculate"),
    c("10 000 рублей в долларах", "convert_currency"),
    c("какой курс лари", "convert_currency"),
    c("сколько я потратил на ИИ за неделю", "show_usage"),
    # --- shopping ---
    c("купить молоко хлеб и яйца", "shopping_add"),
    c("что купить", "shopping_show|shopping_trip"),
    c("иду в магазин", "shopping_trip"),
    c("перенеси покупки из задач в список", "shopping_from_tasks"),
    # --- ideas / notes ---
    c("идея: бот для записи к барберу через телеграм", "capture_idea"),
    c("запиши заметку код от домофона 1234", "capture_note"),
    c("покажи заметки", "show_notes"),
    c("найди идеи про бота", "search_ideas"),
    # --- research ---
    c("изучи лучшие роботы-пылесосы 2026 года", "research"),
    c("найди в интернете сколько стоит виза в Японию", "research"),
    # --- Mac / sites / calls ---
    c("открой телеграм на маке", "mac_open"),
    c("громкость 30", "mac_volume"),
    c("скопируй на мак: привет как дела", "mac_copy"),
    c("напомни на маке через 10 минут выключить чайник", "mac_remind"),
    c("открой claude code в проекте jarvis", "claude_code"),
    c("открой сайт TLT", "site_open"),
    c("какие сайты запущены", "sites_list"),
    c("закрой туннель", "tunnel_close"),
    c("позвони мне через 10 минут", "call_me"),
    c("набери меня в 7:30 и разбуди", "call_me"),
    c("отвечай текстом", "voice_mode"),
    c("статус", "status"),
    c("помощь", "help"),
    c("погода завтра", "weather"),
    c("нужен ли зонт", "weather"),
    # --- several requests in one ---
    c(
        "перенеси стрижку на завтра и напомни в 9 купить хлеб",
        "schedule_referenced_task",
        "remind|add_task",
        kind="multi",
    ),
    c("спал 8 часов, выпил утренние и съел овсянку", "tracker_log+med_taken+food_log|tracker_log", kind="multi"),
    c("что у меня завтра и какая будет погода", "show_agenda", "weather", kind="multi"),
    # --- free speech: should be understood without exact words ---
    c("слушай я что-то совсем вымотался сегодня, спал часов пять", "tracker_log", kind="free"),
    c("короче завтра надо будет заехать в сервис за машиной", "add_task|remind", kind="free"),
    c("ой я забыл выпить таблетки утром, выпил только сейчас", "med_taken", kind="free"),
    c("мне кажется я сегодня переел", "small_talk|ask|food_show|food_log|tracker_log|unknown", kind="free"),
    c("а напомни потом про это", "remind|add_task|unknown", kind="free"),
    # --- questions about the user's data: the question agent (or a specific show intent) ---
    c("свободен ли я завтра после пяти", "ask|show_agenda", kind="data"),
    c("как я спал на этой неделе по сравнению с прошлой", "ask|tracker_show", kind="data"),
    c("хватит ли мне денег до конца месяца", "ask|expense_show", kind="data"),
    c("когда я последний раз взвешивался", "ask|health_show", kind="data"),
    # --- general questions and chat: answered, not saved as tasks ---
    c("привет джарвис как дела", "small_talk", kind="general"),
    c("сколько километров от Тбилиси до Еревана", "ask|small_talk|research", kind="general"),
    c("почему небо голубое", "ask|small_talk|research", kind="general"),
    c("расскажи что-нибудь интересное про космос", "ask|small_talk", kind="general"),
    c("как лучше учить английский", "ask|small_talk|research", kind="general"),
    c("что такое ферритин", "ask|small_talk|labs_show", kind="general"),
    c("спасибо", "small_talk", kind="general"),
    c("ты кто вообще", "small_talk", kind="general"),
    c("переведи на английский я опаздываю на встречу", "ask|small_talk", kind="general"),
    c("придумай название для кофейни", "ask|small_talk|capture_idea", kind="general"),
]
