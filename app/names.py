"""Нормализация имён: «ООО «Ритейл-Групп»» и «Ритейл Групп» — одна организация."""

from __future__ import annotations

import re

_FORMS = r"\b(ооо|оао|зао|пао|ао|ип|тд|тк|ук|гк|ltd|llc|inc|group)\b"
_KINDS = (
    "компания|сеть клиник|сеть|клиника|клиники|стоматология|пекарни|автоцентр|фитнес|юрфирма|агрокомплекс|"
    "мебельная фабрика|фабрика|кофейни|школа английского|школа|отель|сеть азс|детский центр|типография|завод|"
    "салоны|ветклиника|пансионат|студия"
)


def norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'“”„]", " ", s)
    s = re.sub(r"[-–—_.,:;()!?/\\]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def org_key(name: str) -> str:
    """Ключ без формы собственности и пробелов: «ООО «Ритейл-Групп»» → «ритейлгрупп»."""
    s = norm(name)
    s = re.sub(_FORMS, " ", s)
    return re.sub(r"\s+", "", s)


def org_core(name: str) -> str:
    """Ядро имени без вида деятельности: «Стоматология «Улыбка»» → «улыбка». Только для подсказки, не для слияния."""
    raw = name or ""
    m = re.search(r"[«\"“](.+?)[»\"”]", raw)
    if m:
        return org_key(m.group(1))
    s = norm(raw)
    s = re.sub(_FORMS, " ", s)
    s = re.sub(rf"^({_KINDS})\s+", "", s)
    return re.sub(r"\s+", "", s)


def person_key(name: str) -> str:
    return re.sub(r"\s+", " ", norm(name))


def _name_order(words: list[str]) -> list[str]:
    """«Белов Игорь Семёнович» (как в выписке) → «Игорь Семёнович Белов»: отчество вторым словом — значит фамилия впереди."""
    if len(words) == 3 and _patronymic(words[2]) and not _patronymic(words[1]):
        return [words[1], words[2], words[0]]
    return words


def same_person(a: str, b: str) -> bool:
    """«Елена Власова» = «Е. Власова» = «Елена Павловна Власова» = «Власова Елена Павловна». Разные люди с одним именем — не склеиваем."""
    ka, kb = _name_order(person_key(a).split()), _name_order(person_key(b).split())
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    rest_a = {w for w in ka[1:] if len(w) > 1 and not _patronymic(w)}
    rest_b = {w for w in kb[1:] if len(w) > 1 and not _patronymic(w)}
    if not (rest_a & rest_b):
        return False  # нет общей фамилии
    return ka[0][0] == kb[0][0]


def _patronymic(w: str) -> bool:
    return w.endswith(("вич", "вна", "ична", "чна"))
