"""HTTP contracts for Xiaohongshu keyword searches."""
from dataclasses import Field
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, HttpUrl

from service.integrations.xiaohongshu import search_xhs_attractions_text, search_xhs_attractions_images
from service.xiaohongshu_models import TextSearchResponse


class ImageResult(BaseModel):
    title: str = ""
    note_url: str
    image_urls: list[str] = Field(default_factory=list)


class ImageSearchResponse(BaseModel):
    success: Literal[True] = True
    results: list[ImageResult]


class SearchUnavailable(BaseModel):
    success: Literal[False] = False
    message: str


router = APIRouter(
    prefix="/api/xiaohongshu",
    tags=["小红书"],
    responses={501: {"model": SearchUnavailable, "description": "搜索功能尚未实现"}},
)

Keyword = Annotated[str, Query(min_length=1, max_length=200, pattern=r"\S", description="搜索关键词")]


@router.get("/search/text", response_model=TextSearchResponse, summary="按关键词查询小红书文本")
def search_text(keyword: Keyword) -> TextSearchResponse:
    return search_xhs_attractions_text(keyword)


@router.get("/search/images", response_model=ImageSearchResponse, summary="按关键词查询小红书图片")
def search_images(keyword: Keyword):
    raise search_xhs_attractions_images(keyword)
