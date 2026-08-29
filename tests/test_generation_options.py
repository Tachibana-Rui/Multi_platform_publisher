from __future__ import annotations

from pydantic import ValidationError

from app.doubao import _parse_copy, _prioritized_cosplay_tags, build_generation_prompt
from app.models import Post, Tag
from app.schemas import GenerateCopyRequest


def test_generation_request_requires_at_least_one_target():
    assert GenerateCopyRequest(generate_title=True, generate_body=False).generate_title
    assert GenerateCopyRequest(generate_title=False, generate_body=True).generate_body
    try:
        GenerateCopyRequest(generate_title=False, generate_body=False)
    except ValidationError:
        pass
    else:
        raise AssertionError("a generation request must select a target")


def test_single_target_prompt_keeps_the_other_field_empty():
    post = Post(title="时崎狂三灵装", tags=[Tag(name="时崎狂三")])
    assert build_generation_prompt(post, "douyin", 0, generate_title=True, generate_body=False).endswith(
        "本次仅生成标题；正文必须返回空字符串。"
    )
    assert build_generation_prompt(post, "douyin", 0, generate_title=False, generate_body=True).endswith(
        "本次仅生成正文；标题必须返回空字符串。"
    )


def test_cosplay_tags_put_character_outfit_work_and_cosplay_first():
    post = Post(
        title="时崎狂三 灵装",
        tags=[Tag(name="时崎狂三"), Tag(name="灵装"), Tag(name="约会大作战")],
    )
    priorities = _prioritized_cosplay_tags(post)
    assert priorities == ["时崎狂三cos", "时崎狂三灵装", "约会大作战", "cosplay"]

    result = _parse_copy(
        '{"title":"狂三灵装","body":"今天也要优雅登场。","tags":["二次元","角色扮演"]}',
        priority_tags=priorities,
        platform="douyin",
    )
    assert result["tags"][:4] == priorities
    assert result["body"].endswith("#时崎狂三cos #时崎狂三灵装 #约会大作战 #cosplay #二次元")


def test_title_only_result_does_not_append_tags_to_the_empty_body():
    result = _parse_copy(
        '{"title":"只更新标题","body":"","tags":["cosplay"]}',
        require_title=True,
        require_body=False,
        append_tags_to_body=False,
    )
    assert result["title"] == "只更新标题"
    assert result["body"] == ""
