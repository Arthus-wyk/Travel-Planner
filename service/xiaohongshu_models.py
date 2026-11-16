"""Shared response models for Xiaohongshu text search."""

from typing import Literal

from pydantic import BaseModel, HttpUrl


class TextResult(BaseModel):
    title: str
    text: str
    note_url: HttpUrl


class TextSearchResponse(BaseModel):
    success: Literal[True] = True
    results: list[TextResult]
