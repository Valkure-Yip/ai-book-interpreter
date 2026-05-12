"""Derive a StyleGuide from BookOverview."""

from __future__ import annotations

from abi.prompts import get_registry
from abi.providers.llm.factory import LLMRouter, system_message, user_message
from abi.survey._schemas import StyleGuideOutput
from abi.types.run import StyleConfig
from abi.types.survey import BookOverview, StyleGuide

_DEFAULT_DIRECTIVES_ZH = {
    "academic-formal": [
        "使用书面学术汉语，避免口语化表达",
        "保留作者的论证连接词（therefore→因此、however→然而、moreover→此外）",
        "被动语态酌情转换为主动，符合汉语习惯",
        "长句按汉语习惯切分，但保持逻辑连接清晰",
        "首次出现的术语在括号中保留原文，如「具身（embodiment）」",
    ],
    "academic-accessible": [
        "使用清晰的书面汉语，可读性优先",
        "保留学术严谨性，但允许必要的解释性插入",
        "复杂概念首次出现时给出简短释义",
    ],
    "popular-science": [
        "面向普通读者，避免过度专业化",
        "保留例证与类比的鲜活语气",
    ],
    "textbook": [
        "保留教材的循序渐进风格",
        "重要定义、定理、公式严格直译",
    ],
}


async def derive_style_guide(
    *,
    router: LLMRouter,
    overview: BookOverview,
    style: StyleConfig,
    target_language: str,
) -> StyleGuide:
    registry = get_registry()
    register = style.register_override or overview.register  # type: ignore[arg-type]
    prompt = registry.render(
        "style_guide_deriver",
        target_language=target_language,
        title=overview.title,
        thesis=overview.thesis,
        target_audience=overview.target_audience,
        register=register,
        tone_notes=overview.tone_notes,
    )
    messages = [
        system_message("You output strict JSON only."),
        user_message(prompt),
    ]
    try:
        parsed, _ = await router.invoke_structured(
            StyleGuideOutput,
            messages,
            agent_name="style_guide_deriver",
            prompt_version=registry.version_for("style_guide_deriver"),
            metadata={"book_id": overview.book_id, "register": register},
        )
        register_directives = parsed.register_directives or _DEFAULT_DIRECTIVES_ZH.get(
            register, _DEFAULT_DIRECTIVES_ZH["academic-formal"]
        )
        forbidden = parsed.forbidden_patterns
        preferred = parsed.preferred_patterns
    except Exception:
        register_directives = _DEFAULT_DIRECTIVES_ZH.get(
            register, _DEFAULT_DIRECTIVES_ZH["academic-formal"]
        )
        forbidden = []
        preferred = []

    return StyleGuide(
        book_id=overview.book_id,
        target_language=target_language,
        register=register,  # type: ignore[arg-type]
        register_directives=register_directives,
        forbidden_patterns=forbidden,
        preferred_patterns=preferred,
        quote_style=style.quote_style,
        number_style="preserve",
    )
