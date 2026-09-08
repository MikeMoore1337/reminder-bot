from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.callbacks import CallbackAction, CallbackOrigin, CallbackTarget, encode_callback


def revoke_membership_kb(membership_id: int, revision: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отозвать доступ",
                    callback_data=encode_callback(
                        CallbackAction.REVOKE,
                        CallbackTarget.MEMBERSHIP,
                        membership_id,
                        revision,
                        origin=CallbackOrigin.SHARED,
                    ),
                )
            ]
        ]
    )


def revoke_invite_kb(invite_id: int, revision: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отозвать ссылку",
                    callback_data=encode_callback(
                        CallbackAction.REVOKE,
                        CallbackTarget.INVITE,
                        invite_id,
                        revision,
                        origin=CallbackOrigin.SHARED,
                    ),
                )
            ]
        ]
    )
