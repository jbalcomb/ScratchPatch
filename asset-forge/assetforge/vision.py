"""Ask a vision model about images through an OpenAI-compatible chat endpoint.

llama.cpp's llama-server (started with a vision model and its --mmproj file), Ollama and LM Studio all serve
/v1/chat/completions, so one client covers them. Standard library only.
"""

import base64
import io
import json
import re
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "http://localhost:8080/v1"      # llama-server's default port


class VisionError(RuntimeError):
    def __init__(self, message, reply=None):
        super().__init__(message)
        self.reply = reply          # the model's text, when the trouble is with what it said


def png_data_url(image):
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def chat(endpoint, model, system, text, images, json_mode=True, temperature=0.2, timeout=600, max_tokens=1024,
         extra=None):
    """Send one user turn (text plus PIL images) and return the reply text. A reply cut off at max_tokens raises
    VisionError (with the text in .reply): a model stuck repeating itself would otherwise run to the context limit.
    extra: further request fields, e.g. llama-server's repeat_penalty or seed."""
    content = [{"type": "text", "text": text}]
    content += [{"type": "image_url", "image_url": {"url": png_data_url(img)}} for img in images]
    body = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        **(extra or {}),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.load(resp)
    except urllib.error.HTTPError as e:
        raise VisionError(f"{endpoint} answered {e.code}: {e.read()[:500].decode(errors='replace')}") from e
    except urllib.error.URLError as e:
        raise VisionError(f"cannot reach {endpoint}: {e.reason}") from e
    try:
        choice = reply["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise VisionError(f"unexpected reply from {endpoint}: {json.dumps(reply)[:500]}") from e
    if choice.get("finish_reason") == "length":
        raise VisionError(f"reply cut off at {max_tokens} tokens", reply=content)
    return content


def parse_json_reply(text):
    """Return the JSON object in a model reply, tolerating code fences and text around it."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise VisionError(f"reply is not JSON ({len(text)} characters)", reply=text)
