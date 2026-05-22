import json
import base64
import anthropic
from pathlib import Path
from typing import List
from utils.config_loader import get_env
from utils.logger import get_logger

log = get_logger("claude_client")


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=get_env("ANTHROPIC_API_KEY"))


def call_claude(client: anthropic.Anthropic, system: str, user: str) -> dict:
    """Call Claude and return parsed JSON response."""
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    return _parse_json(response.content[0].text)


def call_claude_vision(
    client: anthropic.Anthropic,
    system: str,
    text_prompt: str,
    image_paths: List[str]
) -> dict:
    """
    Call Claude with text + images.
    image_paths: list of local file paths to screenshots.
    """
    content = []

    # Add images first
    for path in image_paths:
        img_path = Path(path)
        if not img_path.exists():
            log.warning(f"Screenshot not found: {path}")
            continue
        with open(img_path, "rb") as f:
            raw = f.read()
        img_data = base64.standard_b64encode(raw).decode("utf-8")
        # Detect media type from magic bytes (most reliable)
        if raw[:4] == b'\x89PNG':
            media_type = "image/png"
        elif raw[:2] in (b'\xff\xd8', b'\xff\xe0', b'\xff\xe1'):
            media_type = "image/jpeg"
        elif raw[:4] == b'GIF8':
            media_type = "image/gif"
        elif raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
            media_type = "image/webp"
        else:
            # Fallback: guess from extension
            ext = img_path.suffix.lower()
            media_type = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif",  ".webp": "image/webp"
            }.get(ext, "image/jpeg")
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": img_data
            }
        })

    # Add text prompt
    content.append({"type": "text", "text": text_prompt})

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": content}]
    )
    return _parse_json(response.content[0].text)


def _parse_json(text: str) -> dict:
    """Strip markdown fences and parse JSON."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())
