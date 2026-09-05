"""The company profile: whose point of view decides `priority`.

A profile version is a prompt fragment, so it is versioned rather than edited in
place — a card always says which version judged it (`items.profile_version`).
"""

from __future__ import annotations

from ..models import CompanyProfile

DEFAULT_NAME = "ООО «Цифра» (GS Labs)"

# Facts from context/company_info.md and context/sources_for_company.md.
DEFAULT_PAYLOAD: dict = {
    "industry": "ИТ, разработка программного обеспечения для платного и спутникового ТВ",
    "products": [
        "CAS/DRM: DREGUARD, DreCrypt, Dre+",
        "middleware и платформа StingrayTV",
        "OTT-платформа Dream Platform",
        "телегид DREGUIDE",
        "умный дом",
        "направление ИИ",
    ],
    "stack": ["цифровое и спутниковое ТВ", "OTT и стриминг", "защита контента", "приставки"],
    "regime": "аккредитованная ИТ-компания, не МСП; продукты в реестре отечественного ПО",
    "regulators": ["Минцифры", "Роскомнадзор", "ФСТЭК", "ФСБ", "Правительство РФ"],
    "competitors": ["производители CAS/DRM и middleware для платного ТВ"],
    "topics": [
        "реестр отечественного ПО",
        "ИТ-льготы и аккредитация",
        "КИИ",
        "персональные данные",
        "импортозамещение",
        "регулирование вещания и платного ТВ",
        "защита контента",
    ],
    "negative_facets": ["МСП", "медтех", "агротех", "розничная торговля", "строительство"],
}


def ensure_default(db) -> CompanyProfile:
    """Seed the built-in profile once; later runs reuse whatever is stored."""
    existing = db.profiles.default()
    if existing is not None:
        return existing
    profile = db.profiles.save(
        CompanyProfile(name=DEFAULT_NAME, payload=DEFAULT_PAYLOAD, is_default=True)
    )
    db.profiles.set_default(profile.id)
    profile.is_default = True
    return profile


def resolve(db, profile_id: int | None = None) -> CompanyProfile:
    """The profile a run judges by: the requested one, else the default."""
    if profile_id is not None:
        profile = db.profiles.get(profile_id)
        if profile is None:
            raise ValueError(f"профиль #{profile_id} не найден")
        return profile
    return ensure_default(db)
