"""Send rendered note pages to Claude and get text back.

The rendered pages are handwriting images, so we use Claude's vision input:
each page becomes a base64 PNG content block, followed by the instruction. The
model reads the handwriting and responds (summary, flow diagram, freeform Q&A).
"""

from __future__ import annotations

import base64

import anthropic

# Cap how many page-images we send in one call, to keep payloads sane. A very
# long notebook is unusual for a single brainstorm; we warn rather than truncate
# silently (see cli.py).
MAX_PAGES = 20

SUMMARY_PROMPT = (
    "These images are pages of my handwritten notes from a reMarkable tablet, "
    "in order. Read the handwriting and write a clear, well-structured summary: "
    "start with a one-line gist, then the key points as bullets, then any open "
    "questions or action items you can infer. Preserve my terminology."
)

DIAGRAM_PROMPT = (
    "These images are pages of my handwritten notes from a reMarkable tablet, in "
    "order. Read the handwriting and turn the ideas into a flow diagram using "
    "Mermaid syntax. Respond with ONLY a single ```mermaid code block (a "
    "flowchart, e.g. `flowchart TD`), no prose before or after. Use my own "
    "labels from the notes for the nodes."
)


class LLMError(RuntimeError):
    pass


def _client(api_key: str | None) -> anthropic.Anthropic:
    # anthropic.Anthropic() also resolves ANTHROPIC_API_KEY / an `ant` profile
    # itself, but we pass an explicit key when the config provided one.
    if api_key:
        return anthropic.Anthropic(api_key=api_key)
    return anthropic.Anthropic()


def _image_blocks(pngs: list[bytes]) -> list[dict]:
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(p).decode("ascii"),
            },
        }
        for p in pngs
    ]


def ask(
    pngs: list[bytes],
    prompt: str,
    *,
    model: str,
    api_key: str | None = None,
    max_tokens: int = 16000,
) -> str:
    """Send page images + a prompt to Claude; return the text response."""
    if not pngs:
        raise LLMError("No page images to send.")
    client = _client(api_key)
    content = _image_blocks(pngs) + [{"type": "text", "text": prompt}]
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIError as e:
        raise LLMError(f"Claude API error: {e}") from e

    if resp.stop_reason == "refusal":
        raise LLMError("Claude declined to answer this request.")
    return "".join(b.text for b in resp.content if b.type == "text").strip()
