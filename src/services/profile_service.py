"""ProfileService — профиль компании: чья точка зрения решает, что важно.

Профиль попадает в промпт (`CompanyProfile.prompt_block`), поэтому запись сюда —
это управление промптом, а не справочником: `ProfileRepo.save` не правит запись
на месте, а поднимает версию, и карточка навсегда помнит, какой версией она
получена (`items.profile_version`, принцип V конституции).
"""

from __future__ import annotations

from ..config import Config
from ..exceptions import ProfileNotFoundError, ProfileValidationError
from ..models import CompanyProfile
from ..processing import profile as profile_mod
from ..repositories import Database
from ..utils import get_logger

log = get_logger("profiles")

MAX_NAME = 200


class ProfileService:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    def list_profiles(self) -> list[CompanyProfile]:
        return self.db.profiles.list()

    def get_profile(self, profile_id: int) -> CompanyProfile:
        profile = self.db.profiles.get(profile_id)
        if profile is None:
            raise ProfileNotFoundError(f"профиль #{profile_id} не найден")
        return profile

    def active(self) -> CompanyProfile:
        """Профиль, которым считается приоритет прямо сейчас (создаётся при первом спросе)."""
        return profile_mod.resolve(self.db)

    def save(self, name: str, payload: dict) -> CompanyProfile:
        """Создать профиль или поднять его версию. Имя — ключ, версия — история."""
        name = (name or "").strip()
        if not name:
            raise ProfileValidationError("у профиля должно быть имя")
        if len(name) > MAX_NAME:
            raise ProfileValidationError(f"имя профиля длиннее {MAX_NAME} символов")
        if not isinstance(payload, dict) or not payload:
            raise ProfileValidationError("payload профиля — непустой объект")
        saved = self.db.profiles.save(CompanyProfile(name=name, payload=payload))
        log.info("профиль «%s» сохранён как версия %s", saved.name, saved.version)
        return saved

    def set_default(self, profile_id: int) -> CompanyProfile:
        """Сменить профиль по умолчанию — это смена того, что система считает «high»."""
        if not self.db.profiles.set_default(profile_id):
            raise ProfileNotFoundError(f"профиль #{profile_id} не найден")
        log.info("профиль #%s стал профилем по умолчанию", profile_id)
        return self.get_profile(profile_id)
