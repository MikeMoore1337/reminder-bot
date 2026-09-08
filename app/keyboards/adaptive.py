from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.callbacks import CallbackAction, CallbackOrigin, CallbackTarget, encode_callback


def suggestion_kb(suggestion_id: int, revision: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Перенести",
                    callback_data=encode_callback(
                        CallbackAction.SUGGESTION_ACCEPT,
                        CallbackTarget.SUGGESTION,
                        suggestion_id,
                        revision,
                        origin=CallbackOrigin.SUGGESTION,
                    ),
                ),
                InlineKeyboardButton(
                    text="Оставить",
                    callback_data=encode_callback(
                        CallbackAction.SUGGESTION_REJECT,
                        CallbackTarget.SUGGESTION,
                        suggestion_id,
                        revision,
                        origin=CallbackOrigin.SUGGESTION,
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Скрыть подсказку",
                    callback_data=encode_callback(
                        CallbackAction.SUGGESTION_DISMISS,
                        CallbackTarget.SUGGESTION,
                        suggestion_id,
                        revision,
                        origin=CallbackOrigin.SUGGESTION,
                    ),
                )
            ],
        ]
    )
