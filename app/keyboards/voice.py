from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.callbacks import CallbackAction, CallbackOrigin, CallbackTarget, encode_callback


def voice_draft_kb(draft_id: int, revision: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Создать",
                    callback_data=encode_callback(
                        CallbackAction.CREATE,
                        CallbackTarget.VOICE_DRAFT,
                        draft_id,
                        revision,
                        origin=CallbackOrigin.VOICE,
                    ),
                ),
                InlineKeyboardButton(
                    text="✏️ Исправить",
                    callback_data=encode_callback(
                        CallbackAction.EDIT,
                        CallbackTarget.VOICE_DRAFT,
                        draft_id,
                        revision,
                        origin=CallbackOrigin.VOICE,
                    ),
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=encode_callback(
                        CallbackAction.CANCEL,
                        CallbackTarget.VOICE_DRAFT,
                        draft_id,
                        revision,
                        origin=CallbackOrigin.VOICE,
                    ),
                ),
            ]
        ]
    )
