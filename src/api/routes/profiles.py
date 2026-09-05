"""Профиль компании: чем система меряет важность.

Запись здесь — управление промптом: профиль целиком уходит в запрос к модели, а
карточка запоминает версию, которой получена. Поэтому правки не перетирают
запись, а поднимают версию.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, status

from ...dependencies import ProfileServiceDep
from ...models.requests import ProfileSaveRequest
from ...models.responses import ProfileListResponse, ProfileResponse

router = APIRouter(prefix="/profiles", tags=["profiles"])

ProfileId = Annotated[int, Path(description="Идентификатор профиля")]


@router.get("", response_model=ProfileListResponse, summary="Профили компании")
def list_profiles(service: ProfileServiceDep) -> ProfileListResponse:
    profiles = service.list_profiles()
    return ProfileListResponse(profiles=[ProfileResponse.from_domain(p) for p in profiles])


@router.get("/active", response_model=ProfileResponse, summary="Профиль, которым считается приоритет")
def active_profile(service: ProfileServiceDep) -> ProfileResponse:
    return ProfileResponse.from_domain(service.active())


@router.post(
    "",
    response_model=ProfileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать профиль или поднять его версию",
)
def save_profile(payload: ProfileSaveRequest, service: ProfileServiceDep) -> ProfileResponse:
    return ProfileResponse.from_domain(service.save(payload.name, payload.payload))


@router.get("/{profile_id}", response_model=ProfileResponse, summary="Один профиль")
def get_profile(profile_id: ProfileId, service: ProfileServiceDep) -> ProfileResponse:
    return ProfileResponse.from_domain(service.get_profile(profile_id))


@router.post(
    "/{profile_id}/default",
    response_model=ProfileResponse,
    summary="Сделать профилем по умолчанию",
)
def set_default(profile_id: ProfileId, service: ProfileServiceDep) -> ProfileResponse:
    return ProfileResponse.from_domain(service.set_default(profile_id))
